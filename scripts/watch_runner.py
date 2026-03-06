#!/usr/bin/env python3
"""
watch_runner.py -- execute all active watchdog checks, evaluate with LLM, surface alerts.

usage:
    python3 scripts/watch_runner.py [--dry-run] [--id <watch-id>] [--verbose]
"""

import argparse
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────────
SKILL_DIR   = Path(__file__).parent.parent
WATCHES_FILE = SKILL_DIR / "watches.json"
LOG_FILE     = SKILL_DIR / "watch_log.jsonl"
HISTORY_N    = 10   # last N results per watch for LLM context

# ── main ───────────────────────────────────────────────────────────────────────

def load_watches():
    if not WATCHES_FILE.exists():
        print(f"no watches.json found at {WATCHES_FILE}")
        print("run: python3 scripts/add_watch.py  or copy assets/watches.example.json")
        return []
    with open(WATCHES_FILE) as f:
        data = json.load(f)
    return [w for w in data.get("watches", []) if w.get("enabled", True)]


def load_history(watch_id: str) -> list:
    if not LOG_FILE.exists():
        return []
    results = []
    with open(LOG_FILE) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("watch_id") == watch_id:
                    results.append(entry)
            except Exception:
                continue
    return results[-HISTORY_N:]


def append_log(entry: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── checkers ───────────────────────────────────────────────────────────────────

def check_http(watch: dict) -> dict:
    import urllib.request
    import urllib.error

    url     = watch["url"]
    timeout = watch.get("timeout_s", 10)
    expect  = watch.get("expect_status", 200)
    match   = watch.get("body_contains")

    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            body = resp.read(4096).decode("utf-8", errors="replace")
            status = resp.status
            ok = (status == expect)
            if ok and match and match not in body:
                ok = False
                return {"ok": False, "status": status, "elapsed_ms": elapsed_ms,
                        "error": f"body_contains '{match}' not found", "body_sample": body[:200]}
            return {"ok": ok, "status": status, "elapsed_ms": elapsed_ms,
                    "error": None if ok else f"expected {expect}, got {status}"}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {"ok": False, "status": None, "elapsed_ms": elapsed_ms, "error": str(e)}


def check_rss(watch: dict) -> dict:
    import urllib.request
    import xml.etree.ElementTree as ET

    url     = watch["url"]
    keyword = watch.get("keyword")

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(body)
        ns = {}
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        entries = []
        for item in items[:10]:
            title_el = item.find("title")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            desc_el = item.find("description") or item.find("{http://www.w3.org/2005/Atom}summary")
            desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            entries.append({"title": title, "description": desc[:200]})
        matched = []
        if keyword:
            kw = keyword.lower()
            for e in entries:
                if kw in e["title"].lower() or kw in e["description"].lower():
                    matched.append(e)
        return {
            "ok": True,
            "entry_count": len(entries),
            "entries": entries[:5],
            "keyword_matches": matched if keyword else None,
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "entry_count": 0, "entries": [], "error": str(e)}


def check_system_disk(watch: dict) -> dict:
    path    = watch.get("path", "/")
    warn_pct = watch.get("warn_percent", 80)
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        pct = round(used / total * 100, 1)
        return {"ok": pct < warn_pct, "used_percent": pct, "free_gb": round(free / 1e9, 2),
                "total_gb": round(total / 1e9, 2), "error": None}
    except Exception as e:
        return {"ok": False, "used_percent": None, "error": str(e)}


def check_system_cpu(watch: dict) -> dict:
    warn_pct = watch.get("warn_percent", 85)
    try:
        import psutil
        pct = psutil.cpu_percent(interval=1)
        return {"ok": pct < warn_pct, "cpu_percent": pct, "error": None}
    except ImportError:
        pass
    try:
        def read_stat():
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            return idle, total
        i1, t1 = read_stat()
        time.sleep(1)
        i2, t2 = read_stat()
        pct = round(100 * (1 - (i2 - i1) / (t2 - t1)), 1)
        return {"ok": pct < warn_pct, "cpu_percent": pct, "error": None}
    except Exception as e:
        return {"ok": False, "cpu_percent": None, "error": str(e)}


def check_system_memory(watch: dict) -> dict:
    warn_pct = watch.get("warn_percent", 85)
    try:
        import psutil
        vm = psutil.virtual_memory()
        pct = vm.percent
        return {"ok": pct < warn_pct, "used_percent": pct,
                "free_gb": round(vm.available / 1e9, 2),
                "total_gb": round(vm.total / 1e9, 2), "error": None}
    except ImportError:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k.strip()] = int(v.strip().split()[0])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        pct = round((total - avail) / total * 100, 1)
        return {"ok": pct < warn_pct, "used_percent": pct,
                "free_gb": round(avail / 1e6, 2),
                "total_gb": round(total / 1e6, 2), "error": None}
    except Exception as e:
        return {"ok": False, "used_percent": None, "error": str(e)}


def check_file(watch: dict) -> dict:
    import hashlib
    path = watch.get("path")
    if not path:
        return {"ok": False, "error": "no path specified"}
    p = Path(path)
    if not p.exists():
        must_exist = watch.get("must_exist", True)
        return {"ok": not must_exist, "exists": False, "error": None if not must_exist else "file not found"}
    stat = p.stat()
    age_s = time.time() - stat.st_mtime
    result = {
        "ok": True, "exists": True,
        "size_bytes": stat.st_size,
        "age_seconds": int(age_s),
        "error": None,
    }
    max_age_s = watch.get("max_age_seconds")
    if max_age_s and age_s > max_age_s:
        result["ok"] = False
        result["error"] = f"file is {int(age_s)}s old, max is {max_age_s}s"
    grep = watch.get("grep")
    if grep:
        try:
            content = p.read_text(errors="replace")
            result["grep_match"] = grep in content
            if watch.get("grep_must_match") and not result["grep_match"]:
                result["ok"] = False
                result["error"] = f"grep '{grep}' not found in file"
        except Exception as e:
            result["grep_match"] = None
            result["error"] = str(e)
    return result


def check_command(watch: dict) -> dict:
    cmd         = watch.get("command")
    expect_exit = watch.get("expect_exit_code", 0)
    stdout_match = watch.get("stdout_contains")
    timeout     = watch.get("timeout_s", 15)

    if not cmd:
        return {"ok": False, "error": "no command specified"}

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        ok = (proc.returncode == expect_exit)
        stdout_sample = proc.stdout[:500]
        match_found = None
        if stdout_match:
            match_found = stdout_match in proc.stdout
            if ok and not match_found:
                ok = False
        return {
            "ok": ok,
            "exit_code": proc.returncode,
            "elapsed_ms": elapsed_ms,
            "stdout_sample": stdout_sample,
            "stdout_contains_match": match_found,
            "stderr_sample": proc.stderr[:200],
            "error": None if ok else f"exit {proc.returncode}, expected {expect_exit}",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "exit_code": None, "error": str(e)}


def check_process(watch: dict) -> dict:
    name     = watch.get("name")
    pid_file = watch.get("pid_file")

    if pid_file:
        pf = Path(pid_file)
        if not pf.exists():
            return {"ok": False, "error": f"pid file not found: {pid_file}", "pid": None}
        try:
            pid = int(pf.read_text().strip())
        except Exception as e:
            return {"ok": False, "error": f"could not read pid file: {e}", "pid": None}
        proc_path = Path(f"/proc/{pid}")
        if proc_path.exists():
            return {"ok": True, "pid": pid, "source": "pid_file", "error": None}
        try:
            import psutil
            if psutil.pid_exists(pid):
                return {"ok": True, "pid": pid, "source": "pid_file", "error": None}
        except ImportError:
            pass
        return {"ok": False, "pid": pid, "error": f"pid {pid} from pid file is not running", "source": "pid_file"}

    if name:
        try:
            import psutil
            matches = [p.info for p in psutil.process_iter(["pid", "name", "cmdline"])
                       if name.lower() in (p.info["name"] or "").lower()
                       or any(name.lower() in (c or "").lower() for c in (p.info["cmdline"] or []))]
            if matches:
                return {"ok": True, "pid": matches[0]["pid"], "match_count": len(matches),
                        "source": "psutil", "error": None}
            return {"ok": False, "pid": None, "match_count": 0,
                    "source": "psutil", "error": f"no process matching '{name}' found"}
        except ImportError:
            pass
        try:
            result = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
            pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip()]
            ok = len(pids) > 0
            return {"ok": ok, "pids": pids, "match_count": len(pids),
                    "source": "pgrep",
                    "error": None if ok else f"no process matching '{name}' found"}
        except Exception as e:
            return {"ok": False, "error": str(e), "pid": None}

    return {"ok": False, "error": "must specify 'name' or 'pid_file'"}


def check_port(watch: dict) -> dict:
    import socket

    host    = watch.get("host", "localhost")
    port    = watch.get("port")
    timeout = watch.get("timeout_s", 5)

    if port is None:
        return {"ok": False, "error": "no port specified"}

    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return {"ok": True, "host": host, "port": port, "elapsed_ms": elapsed_ms, "error": None}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {"ok": False, "host": host, "port": port, "elapsed_ms": elapsed_ms, "error": str(e)}


def check_ssl_cert(watch: dict) -> dict:
    import ssl
    import socket

    host     = watch.get("host")
    port     = watch.get("port", 443)
    warn_days = watch.get("warn_days", 14)

    if not host:
        return {"ok": False, "error": "no host specified"}

    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
            s.settimeout(10)
            s.connect((host, port))
            cert = s.getpeercert()
        expire_str = cert["notAfter"]
        expire_dt = datetime.strptime(expire_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_left = (expire_dt - now).days
        ok = days_left >= warn_days
        return {
            "ok": ok,
            "host": host,
            "days_until_expiry": days_left,
            "expires": expire_str,
            "error": None if ok else f"cert expires in {days_left} days (warn threshold: {warn_days})",
        }
    except Exception as e:
        return {"ok": False, "host": host, "days_until_expiry": None, "error": str(e)}


def check_json_api(watch: dict) -> dict:
    """
    poll a json api and evaluate a field with a condition.

    watch config fields:
        url           -- the api endpoint to GET
        field         -- dot-notation path to the value to extract, e.g. "status" or "data.health"
        condition     -- expression to evaluate, e.g. '!= "ok"' or '> 100' or '== true'
        headers       -- optional dict of request headers (e.g. for auth)
        timeout_s     -- request timeout in seconds (default 10)
        expect_status -- expected http status code (default 200)

    the condition is evaluated as:  <extracted_value> <condition>
    if the condition is true, ok=False (meaning the watch is triggered / something is wrong).
    if the condition is false, ok=True (everything looks fine).

    examples:
        field: "status",        condition: '!= "ok"'   -- alert when status is not "ok"
        field: "queue_depth",   condition: "> 1000"    -- alert when queue is too deep
        field: "healthy",       condition: "== false"  -- alert when healthy is false
        field: "error_rate",    condition: ">= 0.05"   -- alert when error rate >= 5%
    """
    import urllib.request
    import urllib.error

    url          = watch.get("url")
    field        = watch.get("field")
    condition    = watch.get("condition")
    headers      = watch.get("headers", {})
    timeout      = watch.get("timeout_s", 10)
    expect_status = watch.get("expect_status", 200)

    if not url:
        return {"ok": False, "error": "no url specified"}
    if not field:
        return {"ok": False, "error": "no field specified"}
    if not condition:
        return {"ok": False, "error": "no condition specified"}

    # fetch the api
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            status = resp.status
            body = resp.read(65536).decode("utf-8", errors="replace")
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "url": url,
            "field": field,
            "condition": condition,
            "field_value": None,
            "condition_triggered": None,
            "elapsed_ms": elapsed_ms,
            "error": str(e),
        }

    if status != expect_status:
        return {
            "ok": False,
            "url": url,
            "field": field,
            "condition": condition,
            "http_status": status,
            "field_value": None,
            "condition_triggered": None,
            "elapsed_ms": elapsed_ms,
            "error": f"expected http {expect_status}, got {status}",
        }

    # parse json
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "url": url,
            "field": field,
            "condition": condition,
            "field_value": None,
            "condition_triggered": None,
            "elapsed_ms": elapsed_ms,
            "error": f"json parse error: {e}",
            "body_sample": body[:300],
        }

    # extract field via dot notation
    field_value, extract_error = _extract_field(data, field)
    if extract_error:
        return {
            "ok": False,
            "url": url,
            "field": field,
            "condition": condition,
            "http_status": status,
            "field_value": None,
            "condition_triggered": None,
            "elapsed_ms": elapsed_ms,
            "error": extract_error,
        }

    # evaluate the condition
    condition_triggered, eval_error = _eval_condition(field_value, condition)
    if eval_error:
        return {
            "ok": False,
            "url": url,
            "field": field,
            "condition": condition,
            "http_status": status,
            "field_value": field_value,
            "condition_triggered": None,
            "elapsed_ms": elapsed_ms,
            "error": eval_error,
        }

    # condition_triggered=True means the bad condition fired -- watch is not ok
    ok = not condition_triggered

    return {
        "ok": ok,
        "url": url,
        "field": field,
        "field_value": field_value,
        "condition": condition,
        "condition_triggered": condition_triggered,
        "http_status": status,
        "elapsed_ms": elapsed_ms,
        "error": None if ok else f"condition '{field} {condition}' triggered (value: {repr(field_value)})",
    }


def _extract_field(data: dict, field_path: str):
    """
    extract a value from a nested dict using dot notation.
    returns (value, error_string_or_none).
    supports simple keys and integer list indices, e.g. "results.0.status".
    """
    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return None, f"field '{part}' not found in json (path: {field_path})"
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                return None, f"cannot index list with '{part}' (path: {field_path})"
        else:
            return None, f"cannot traverse into {type(current).__name__} at '{part}' (path: {field_path})"
    return current, None


def _eval_condition(value, condition: str):
    """
    evaluate a condition string against a value.
    condition is an operator + operand, e.g. '!= "ok"' or '> 100' or '== false'.

    supported operators: ==, !=, >, >=, <, <=, contains, not_contains

    returns (triggered: bool, error: str or None).
    triggered=True means the condition fired (something is wrong).
    """
    condition = condition.strip()

    # special string operators
    if condition.startswith("contains "):
        operand = condition[len("contains "):].strip().strip('"').strip("'")
        if not isinstance(value, str):
            return None, f"'contains' requires a string field, got {type(value).__name__}"
        return operand in value, None

    if condition.startswith("not_contains "):
        operand = condition[len("not_contains "):].strip().strip('"').strip("'")
        if not isinstance(value, str):
            return None, f"'not_contains' requires a string field, got {type(value).__name__}"
        return operand not in value, None

    # standard comparison operators
    operators = ["!=", "==", ">=", "<=", ">", "<"]
    op = None
    operand_str = None
    for candidate in operators:
        if condition.startswith(candidate):
            op = candidate
            operand_str = condition[len(candidate):].strip()
            break

    if op is None:
        return None, f"unrecognized condition format: '{condition}' -- expected operator like '== \"ok\"' or '> 100'"

    # parse operand -- handle string literals, booleans, null, numbers
    operand = _parse_operand(operand_str)

    # coerce value for numeric comparisons when sensible
    if op in (">", ">=", "<", "<="):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None, f"operator '{op}' requires a numeric field, got {repr(value)}"
        try:
            operand = float(operand)
        except (TypeError, ValueError):
            return None, f"operator '{op}' requires a numeric operand, got {repr(operand_str)}"

    try:
        if op == "==":
            triggered = value == operand
        elif op == "!=":
            triggered = value != operand
        elif op == ">":
            triggered = value > operand
        elif op == ">=":
            triggered = value >= operand
        elif op == "<":
            triggered = value < operand
        elif op == "<=":
            triggered = value <= operand
        else:
            return None, f"unknown operator '{op}'"
    except TypeError as e:
        return None, f"comparison failed: {e}"

    return triggered, None


def _parse_operand(operand_str: str):
    """
    parse a condition operand string into a python value.
    handles: quoted strings, true/false, null/none, integers, floats.
    """
    s = operand_str.strip()

    # quoted string
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]

    # booleans
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False

    # null
    if s.lower() in ("null", "none"):
        return None

    # numeric
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass

    # fallback: return as-is string
    return s


# ── dispatch ───────────────────────────────────────────────────────────────────

CHECKERS = {
    "http":          check_http,
    "rss":           check_rss,
    "system_disk":   check_system_disk,
    "system_cpu":    check_system_cpu,
    "system_memory": check_system_memory,
    "file":          check_file,
    "command":       check_command,
    "process":       check_process,
    "port":          check_port,
    "ssl_cert":      check_ssl_cert,
    "json_api":      check_json_api,
}


def run_check(watch: dict) -> dict:
    wtype = watch.get("type")
    checker = CHECKERS.get(wtype)
    if not checker:
        return {"ok": False, "error": f"unknown watch type: {wtype}"}
    try:
        return checker(watch)
    except Exception as e:
        return {"ok": False, "error": f"checker crashed: {e}"}


# ── llm eval ───────────────────────────────────────────────────────────────────

def llm_evaluate(watch: dict, result: dict, history: list) -> Optional[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    import urllib.request as ur

    prompt = f"""you are a monitoring assistant. evaluate the following watch result and decide if it needs an alert.

watch definition:
{json.dumps(watch, indent=2)}

current result:
{json.dumps(result, indent=2)}

recent history (last {len(history)} runs):
{json.dumps(history, indent=2)}

respond with a json object only, no other text:
{{
  "alert": true or false,
  "severity": "info" | "warning" | "critical",
  "summary": "one line explanation"
}}"""

    payload = json.dumps({
        "model": "claude-3-haiku-20240307",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = ur.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with ur.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        text = body["content"][0]["text"].strip()
        return json.loads(text)
    except Exception:
        return None


# ── runner ─────────────────────────────────────────────────────────────────────

def run_all(watches: list, dry_run: bool = False, verbose: bool = False):
    alerts = []

    for watch in watches:
        wid   = watch.get("id", "unknown")
        wname = watch.get("name", wid)
        wtype = watch.get("type", "?")

        if verbose:
            print(f"  checking [{wtype}] {wname} ...", end=" ", flush=True)

        if dry_run:
            print(f"  [dry-run] would check [{wtype}] {wname}")
            continue

        result  = run_check(watch)
        history = load_history(wid)

        eval_result = llm_evaluate(watch, result, history)

        if eval_result is None:
            # fallback: use ok field directly
            alerted  = not result.get("ok", True)
            severity = "warning" if alerted else "info"
            summary  = result.get("error") or ("ok" if not alerted else "check failed")
            eval_result = {"alert": alerted, "severity": severity, "summary": summary}

        alerted = eval_result.get("alert", False)

        log_entry = {
            "ts":       int(time.time()),
            "watch_id": wid,
            "type":     wtype,
            "result":   result,
            "eval":     eval_result,
            "alerted":  alerted,
        }
        append_log(log_entry)

        if verbose:
            status_str = "ALERT" if alerted else "ok"
            print(f"{status_str} -- {eval_result.get('summary', '')}")

        if alerted:
            alerts.append({"watch": watch, "result": result, "eval": eval_result})

    return alerts


def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true", help="show what would be checked, do not run")
    parser.add_argument("--id",       help="run only the watch with this id")
    parser.add_argument("--verbose",  action="store_true", help="print status for every watch")
    args = parser.parse_args()

    watches = load_watches()
    if not watches:
        sys.exit(0)

    if args.id:
        watches = [w for w in watches if w.get("id") == args.id]
        if not watches:
            print(f"no watch found with id '{args.id}'")
            sys.exit(1)

    if args.verbose or args.dry_run:
        print