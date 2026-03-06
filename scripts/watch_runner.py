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
            body = resp.read()
        root = ET.fromstring(body)
    except Exception as e:
        return {"ok": False, "error": str(e), "new_entries": [], "matched": []}

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = []

    # rss
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        entries.append({"title": title, "link": link, "description": desc[:200]})

    # atom
    for entry in root.findall(".//atom:entry", ns):
        title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link  = link_el.get("href", "") if link_el is not None else ""
        summ  = (entry.findtext("atom:summary", namespaces=ns) or "").strip()
        entries.append({"title": title, "link": link, "description": summ[:200]})

    matched = []
    if keyword:
        kl = keyword.lower()
        matched = [e for e in entries if kl in e["title"].lower() or kl in e["description"].lower()]

    return {
        "ok": True,
        "error": None,
        "entry_count": len(entries),
        "latest_titles": [e["title"] for e in entries[:5]],
        "matched": matched,
    }


def check_system_cpu(watch: dict) -> dict:
    threshold = watch.get("threshold_pct", 90)
    cpu_pct   = None

    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=1)
    except ImportError:
        pass

    if cpu_pct is None:
        try:
            def read_stat():
                with open("/proc/stat") as f:
                    line = f.readline()
                parts = list(map(int, line.split()[1:]))
                idle  = parts[3]
                total = sum(parts)
                return idle, total
            i1, t1 = read_stat()
            time.sleep(1)
            i2, t2 = read_stat()
            cpu_pct = round(100.0 * (1 - (i2 - i1) / (t2 - t1)), 1)
        except Exception as e:
            return {"ok": False, "error": str(e), "cpu_pct": None}

    ok = cpu_pct < threshold
    return {
        "ok": ok,
        "cpu_pct": cpu_pct,
        "threshold_pct": threshold,
        "error": None if ok else f"cpu {cpu_pct}% exceeds threshold {threshold}%",
    }


def check_system_disk(watch: dict) -> dict:
    path      = watch.get("path", "/")
    threshold = watch.get("threshold_pct", 90)
    used_pct  = None

    try:
        import psutil
        usage    = psutil.disk_usage(path)
        used_pct = usage.percent
    except ImportError:
        pass

    if used_pct is None:
        try:
            import shutil
            total, used, free = shutil.disk_usage(path)
            used_pct = round(100.0 * used / total, 1)
        except Exception as e:
            return {"ok": False, "error": str(e), "used_pct": None, "path": path}

    ok = used_pct < threshold
    return {
        "ok": ok,
        "path": path,
        "used_pct": used_pct,
        "threshold_pct": threshold,
        "error": None if ok else f"disk {used_pct}% used at {path}, threshold {threshold}%",
    }


def check_system_memory(watch: dict) -> dict:
    threshold = watch.get("threshold_pct", 90)
    used_pct  = None

    try:
        import psutil
        mem      = psutil.virtual_memory()
        used_pct = mem.percent
    except ImportError:
        pass

    if used_pct is None:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
            total     = info.get("MemTotal", 0)
            available = info.get("MemAvailable", info.get("MemFree", 0))
            if total:
                used_pct = round(100.0 * (total - available) / total, 1)
        except Exception as e:
            return {"ok": False, "error": str(e), "used_pct": None}

    if used_pct is None:
        return {"ok": False, "error": "could not read memory info", "used_pct": None}

    ok = used_pct < threshold
    return {
        "ok": ok,
        "used_pct": used_pct,
        "threshold_pct": threshold,
        "error": None if ok else f"memory {used_pct}% used, threshold {threshold}%",
    }


def check_file(watch: dict) -> dict:
    import hashlib

    path       = watch.get("path")
    must_exist = watch.get("must_exist", True)
    max_age_s  = watch.get("max_age_s")
    grep       = watch.get("contains")
    hash_watch = watch.get("hash")

    if not path:
        return {"ok": False, "error": "no path configured"}

    p = Path(path)
    if not p.exists():
        if must_exist:
            return {"ok": False, "error": f"{path} does not exist", "exists": False}
        return {"ok": True, "exists": False, "error": None}

    stat   = p.stat()
    age_s  = int(time.time() - stat.st_mtime)
    size_b = stat.st_size
    result = {"ok": True, "exists": True, "size_b": size_b, "age_s": age_s, "error": None}

    if max_age_s and age_s > max_age_s:
        result["ok"] = False
        result["error"] = f"{path} last modified {age_s}s ago, max {max_age_s}s"
        return result

    if grep or hash_watch:
        try:
            content = p.read_text(errors="replace")
        except Exception as e:
            result["ok"] = False
            result["error"] = f"could not read file: {e}"
            return result

        if grep and grep not in content:
            result["ok"] = False
            result["error"] = f"string '{grep}' not found in {path}"
            return result

        if hash_watch:
            actual = hashlib.sha256(content.encode()).hexdigest()
            result["hash_sha256"] = actual
            if actual != hash_watch:
                result["ok"] = False
                result["error"] = f"hash mismatch: expected {hash_watch}, got {actual}"
                return result

    return result


def check_command(watch: dict) -> dict:
    cmd            = watch.get("command")
    expect_exit    = watch.get("expect_exit", 0)
    stdout_contains = watch.get("stdout_contains")
    timeout        = watch.get("timeout_s", 30)

    if not cmd:
        return {"ok": False, "error": "no command configured"}

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        exit_code = proc.returncode

        ok = (exit_code == expect_exit)
        error = None

        if not ok:
            error = f"exit code {exit_code}, expected {expect_exit}"

        if ok and stdout_contains and stdout_contains not in stdout:
            ok = False
            error = f"stdout_contains '{stdout_contains}' not found"

        return {
            "ok": ok,
            "exit_code": exit_code,
            "stdout": stdout[:500],
            "stderr": stderr[:200],
            "error": error,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s", "exit_code": None}
    except Exception as e:
        return {"ok": False, "error": str(e), "exit_code": None}


def check_process(watch: dict) -> dict:
    name     = watch.get("name")
    pid_file = watch.get("pid_file")

    if pid_file:
        pid_path = Path(pid_file)
        if not pid_path.exists():
            return {"ok": False, "error": f"pid file {pid_file} not found", "running": False}
        try:
            pid = int(pid_path.read_text().strip())
        except Exception as e:
            return {"ok": False, "error": f"could not read pid file: {e}", "running": False}

        proc_path = Path(f"/proc/{pid}")
        if proc_path.exists():
            return {"ok": True, "running": True, "pid": pid, "error": None}

        try:
            import psutil
            if psutil.pid_exists(pid):
                return {"ok": True, "running": True, "pid": pid, "error": None}
        except ImportError:
            pass

        return {"ok": False, "running": False, "pid": pid,
                "error": f"process with pid {pid} is not running"}

    if not name:
        return {"ok": False, "error": "no name or pid_file configured"}

    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            pname = proc.info.get("name") or ""
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if name.lower() in pname.lower() or name.lower() in cmdline.lower():
                return {"ok": True, "running": True, "pid": proc.pid, "error": None}
    except ImportError:
        pass

    try:
        result = subprocess.run(
            ["pgrep", "-f", name], capture_output=True, text=True
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split()
            return {"ok": True, "running": True, "pid": int(pids[0]), "error": None}
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if name.lower() in line.lower():
                return {"ok": True, "running": True, "error": None}
    except Exception:
        pass

    return {"ok": False, "running": False, "error": f"process '{name}' not found"}


def check_port(watch: dict) -> dict:
    import socket

    host    = watch.get("host", "localhost")
    port    = watch.get("port")
    timeout = watch.get("timeout_s", 5)

    if port is None:
        return {"ok": False, "error": "no port configured"}

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

    host        = watch.get("host")
    port        = watch.get("port", 443)
    warn_days   = watch.get("warn_days", 14)
    timeout     = watch.get("timeout_s", 10)

    if not host:
        return {"ok": False, "error": "no host configured"}

    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((host, port), timeout=timeout),
                             server_hostname=host) as ssock:
            cert = ssock.getpeercert()

        expires_str = cert["notAfter"]
        expires_dt  = datetime.strptime(expires_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        now         = datetime.now(timezone.utc)
        days_left   = (expires_dt - now).days

        ok = days_left >= warn_days
        return {
            "ok": ok,
            "host": host,
            "port": port,
            "days_left": days_left,
            "expires": expires_str,
            "warn_days": warn_days,
            "error": None if ok else f"cert expires in {days_left} days (threshold: {warn_days})",
        }
    except Exception as e:
        return {"ok": False, "host": host, "port": port, "error": str(e)}


def check_json_api(watch: dict) -> dict:
    import urllib.request
    import urllib.error

    url       = watch.get("url")
    field     = watch.get("field")
    condition = watch.get("condition")
    timeout   = watch.get("timeout_s", 10)

    if not url:
        return {"ok": False, "error": "no url configured"}

    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            raw = resp.read()
            data = json.loads(raw)
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {"ok": False, "error": str(e), "elapsed_ms": elapsed_ms}

    result = {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "error": None,
    }

    if field:
        # support dot notation: "data.status"
        keys  = field.split(".")
        value = data
        try:
            for k in keys:
                if isinstance(value, list):
                    value = value[int(k)]
                else:
                    value = value[k]
        except (KeyError, IndexError, TypeError):
            result["ok"] = False
            result["error"] = f"field '{field}' not found in response"
            result["field"] = field
            return result

        result["field"] = field
        result["value"] = value

        if condition:
            ok = _eval_condition(value, condition)
            result["condition"] = condition
            result["condition_met"] = ok
            if not ok:
                result["ok"] = False
                result["error"] = f"field '{field}' = {repr(value)}, condition '{condition}' not met"
    else:
        result["response_sample"] = str(data)[:300]

    return result


def _eval_condition(value, condition: str) -> bool:
    """
    evaluate a simple condition string against a value.
    supported: == != < > <= >= contains
    """
    condition = condition.strip()
    for op in ["<=", ">=", "!=", "==", "<", ">", "contains"]:
        if condition.startswith(op):
            rhs_raw = condition[len(op):].strip().strip('"').strip("'")
            if op == "contains":
                return rhs_raw in str(value)
            try:
                rhs = type(value)(rhs_raw)
            except (ValueError, TypeError):
                rhs = rhs_raw
            ops = {
                "==": lambda a, b: a == b,
                "!=": lambda a, b: a != b,
                "<":  lambda a, b: a < b,
                ">":  lambda a, b: a > b,
                "<=": lambda a, b: a <= b,
                ">=": lambda a, b: a >= b,
            }
            return ops[op](value, rhs)
    return False


def check_ping(watch: dict) -> dict:
    """
    icmp ping check using the system ping command.
    works on linux and macos.

    watch config fields:
      host          (str, required) -- hostname or ip to ping
      count         (int, default 4) -- number of ping packets to send
      timeout_s     (int, default 10) -- total timeout for the ping command
      max_latency_ms (float, optional) -- alert if avg latency exceeds this
      max_loss_pct  (float, default 0) -- alert if packet loss exceeds this percent
    """
    host           = watch.get("host")
    count          = watch.get("count", 4)
    timeout_s      = watch.get("timeout_s", 10)
    max_latency_ms = watch.get("max_latency_ms")
    max_loss_pct   = watch.get("max_loss_pct", 0)

    if not host:
        return {"ok": False, "error": "no host configured", "host": None}

    # build the ping command -- linux and macos have slightly different flags
    # -c count: number of packets
    # -W / -w: deadline/timeout
    # we try linux style first, fall back to macos style on failure
    import platform
    system = platform.system().lower()

    if system == "darwin":
        # macos: -t timeout (per-packet timeout), -c count
        cmd = ["ping", "-c", str(count), "-t", str(timeout_s), host]
    else:
        # linux: -c count, -W timeout (per-packet wait), -w deadline
        cmd = ["ping", "-c", str(count), "-W", str(timeout_s), host]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 5,  # outer python timeout, slightly longer than ping timeout
        )
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "host": host,
            "error": f"ping timed out after {timeout_s + 5}s",
            "packet_loss_pct": 100.0,
            "avg_latency_ms": None,
            "min_latency_ms": None,
            "max_latency_ms_observed": None,
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "host": host,
            "error": "ping command not found -- not supported on this system",
            "packet_loss_pct": None,
            "avg_latency_ms": None,
            "min_latency_ms": None,
            "max_latency_ms_observed": None,
        }
    except Exception as e:
        return {
            "ok": False,
            "host": host,
            "error": str(e),
            "packet_loss_pct": None,
            "avg_latency_ms": None,
            "min_latency_ms": None,
            "max_latency_ms_observed": None,
        }

    # parse packet loss from ping output
    # linux:  "4 packets transmitted, 4 received, 0% packet loss"
    # macos:  "4 packets transmitted, 4 packets received, 0.0% packet loss"
    loss_pct = None
    import re
    loss_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*packet loss", stdout)
    if loss_match:
        loss_pct = float(loss_match.group(1))

    # parse rtt stats from ping output
    # linux:  "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.100 ms"
    # macos:  "round-trip min/avg/max/stddev = 0.123/0.456/0.789/0.100 ms"
    min_ms = avg_ms = max_ms = None
    rtt_match = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)",
        stdout
    )
    if rtt_match:
        min_ms = float(rtt_match.group(1))
        avg_ms = float(rtt_match.group(2))
        max_ms = float(rtt_match.group(3))

    # if ping command returned a non-zero exit and we have no loss info,
    # treat as 100% loss (host unreachable)
    if loss_pct is None:
        loss_pct = 100.0 if proc.returncode != 0 else 0.0

    # determine ok status
    ok = True
    errors = []

    if loss_pct > max_loss_pct:
        ok = False
        errors.append(f"packet loss {loss_pct}% exceeds threshold {max_loss_pct}%")

    if max_latency_ms is not None and avg_ms is not None and avg_ms > max_latency_ms:
        ok = False
        errors.append(f"avg latency {avg_ms}ms exceeds threshold {max_latency_ms}ms")

    if loss_pct == 100.0 and avg_ms is None:
        ok = False
        if not errors:
            errors.append(f"host {host} is unreachable")

    return {
        "ok": ok,
        "host": host,
        "packet_loss_pct": loss_pct,
        "avg_latency_ms": avg_ms,
        "min_latency_ms": min_ms,
        "max_latency_ms_observed": max_ms,
        "packets_sent": count,
        "error": "; ".join(errors) if errors else None,
    }


# ── dispatcher ─────────────────────────────────────────────────────────────────

CHECKERS = {
    "http":          check_http,
    "rss":           check_rss,
    "system_cpu":    check_system_cpu,
    "system_disk":   check_system_disk,
    "system_memory": check_system_memory,
    "file":          check_file,
    "command":       check_command,
    "process":       check_process,
    "port":          check_port,
    "ssl_cert":      check_ssl_cert,
    "json_api":      check_json_api,
    "ping":          check_ping,
}


def run_check(watch: dict) -> dict:
    wtype = watch.get("type")
    if wtype not in CHECKERS:
        return {"ok": False, "error": f"unknown watch type: {wtype}"}
    try:
        return CHECKERS[wtype](watch)
    except Exception as e:
        return {"ok": False, "error": f"checker crashed: {e}"}


# ── llm eval ───────────────────────────────────────────────────────────────────

def llm_evaluate(watch: dict, result: dict, history: list) -> Optional[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return None

    history_snippet = json.dumps(history[-3:], indent=2) if history else "none"
    prompt = f"""you are a monitoring agent evaluating a watchdog check result.

watch definition:
{json.dumps(watch, indent=2)}

current result:
{json.dumps(result, indent=2)}

recent history (last few results):
{history_snippet}

respond with a json object only, no prose:
{{
  "alert": true or false,
  "severity": "info" | "warning" | "critical",
  "summary": "one line explanation"
}}

alert=true means this result needs the user's attention.
be conservative -- only alert if something is actually wrong or noteworthy.
"""

    try:
        msg = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception:
        return None


# ── runner ─────────────────────────────────────────────────────────────────────

def run_all(watches: list, dry_run: bool = False, verbose: bool = False):
    alerts = []

    for watch in watches:
        wid   = watch.get("id", "unknown")
        wtype = watch.get("type", "unknown")
        name  = watch.get("name", wid)

        if verbose:
            print(f"  checking [{wtype}] {name} ...", end=" ", flush=True)

        if dry_run:
            print(f"  [dry-run] would check [{wtype}] {name}")
            continue

        result  = run_check(watch)
        history = load_history(wid)

        eval_result = llm_evaluate(watch, result, history)

        if eval_result is None:
            # fallback: alert if ok=False
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
            status = "ALERT" if alerted else "ok"
            print(f"{status} -- {eval_result.get('summary', '')}")

        if alerted:
            alerts.append({
                "watch": watch,
                "result": result,
                "eval": eval_result,
            })

    return alerts


def print_alerts(alerts: list):
    if not alerts:
        print("watchdog: all clear")
        return

    print(f"\nwatchdog: {len(alerts)} alert(s)\n")
    for a in alerts:
        watch    = a["watch"]
        eval_r   = a["eval"]
        severity = eval_r.get("severity", "warning").upper()
        summary