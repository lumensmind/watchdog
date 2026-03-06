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
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as e:
        return {"ok": False, "error": str(e), "new_entries": []}

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # handle both rss <item> and atom <entry>
    items = root.findall(f".//{ns}item") or root.findall(f".//{ns}entry")

    entries = []
    for item in items[:20]:
        title_el = item.find(f"{ns}title")
        link_el  = item.find(f"{ns}link")
        desc_el  = item.find(f"{ns}description") or item.find(f"{ns}summary")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link  = link_el.text.strip()  if link_el  is not None and link_el.text  else ""
        desc  = desc_el.text.strip()  if desc_el  is not None and desc_el.text  else ""
        entries.append({"title": title, "link": link, "desc": desc[:200]})

    matched = []
    if keyword:
        kw = keyword.lower()
        for e in entries:
            if kw in e["title"].lower() or kw in e["desc"].lower():
                matched.append(e)

    return {
        "ok": True,
        "error": None,
        "entry_count": len(entries),
        "entries_sample": entries[:5],
        "keyword": keyword,
        "keyword_matches": matched if keyword else None,
    }


def check_system_disk(watch: dict) -> dict:
    path    = watch.get("path", "/")
    warn_pct = watch.get("warn_above_pct", 80)

    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        pct = round(used / total * 100, 1)
        return {"ok": pct < warn_pct, "path": path, "used_pct": pct,
                "used_gb": round(used / 1e9, 2), "total_gb": round(total / 1e9, 2),
                "error": None if pct < warn_pct else f"disk {pct}% used (threshold {warn_pct}%)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_system_cpu(watch: dict) -> dict:
    warn_pct = watch.get("warn_above_pct", 90)

    # try psutil first
    try:
        import psutil
        pct = psutil.cpu_percent(interval=1)
        return {"ok": pct < warn_pct, "used_pct": pct, "source": "psutil",
                "error": None if pct < warn_pct else f"cpu {pct}% (threshold {warn_pct}%)"}
    except ImportError:
        pass

    # fallback: /proc/stat two-sample
    try:
        def read_stat():
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            return idle, total

        idle1, total1 = read_stat()
        time.sleep(1)
        idle2, total2 = read_stat()
        diff_idle  = idle2 - idle1
        diff_total = total2 - total1
        pct = round((1 - diff_idle / diff_total) * 100, 1) if diff_total else 0.0
        return {"ok": pct < warn_pct, "used_pct": pct, "source": "/proc/stat",
                "error": None if pct < warn_pct else f"cpu {pct}% (threshold {warn_pct}%)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_system_memory(watch: dict) -> dict:
    warn_pct = watch.get("warn_above_pct", 90)

    try:
        import psutil
        vm = psutil.virtual_memory()
        pct = vm.percent
        return {"ok": pct < warn_pct, "used_pct": pct,
                "used_gb": round(vm.used / 1e9, 2), "total_gb": round(vm.total / 1e9, 2),
                "source": "psutil",
                "error": None if pct < warn_pct else f"memory {pct}% used (threshold {warn_pct}%)"}
    except ImportError:
        pass

    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total     = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used      = total - available
        pct       = round(used / total * 100, 1) if total else 0.0
        return {"ok": pct < warn_pct, "used_pct": pct,
                "used_gb": round(used / 1e6, 2), "total_gb": round(total / 1e6, 2),
                "source": "/proc/meminfo",
                "error": None if pct < warn_pct else f"memory {pct}% used (threshold {warn_pct}%)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_file(watch: dict) -> dict:
    import hashlib

    path        = watch.get("path")
    must_exist  = watch.get("must_exist", True)
    max_age_s   = watch.get("max_age_s")
    grep        = watch.get("contains")

    if not path:
        return {"ok": False, "error": "no path specified"}

    p = Path(path)
    if not p.exists():
        return {"ok": not must_exist, "exists": False,
                "error": f"{path} does not exist" if must_exist else None}

    stat = p.stat()
    age_s = time.time() - stat.st_mtime
    size  = stat.st_size

    result = {"ok": True, "exists": True, "size_bytes": size,
              "age_s": int(age_s), "error": None}

    if max_age_s and age_s > max_age_s:
        result["ok"] = False
        result["error"] = f"file is {int(age_s)}s old (max {max_age_s}s)"

    if grep and result["ok"]:
        try:
            content = p.read_text(errors="replace")
            if grep not in content:
                result["ok"] = False
                result["error"] = f"'{grep}' not found in file"
            else:
                result["grep_found"] = True
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)

    return result


def check_command(watch: dict) -> dict:
    cmd           = watch.get("command")
    expect_exit   = watch.get("expect_exit", 0)
    output_contains = watch.get("output_contains")
    timeout       = watch.get("timeout_s", 30)
    shell         = watch.get("shell", True)

    if not cmd:
        return {"ok": False, "error": "no command specified"}

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True, timeout=timeout
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        exit_ok = (proc.returncode == expect_exit)

        result = {
            "ok": exit_ok,
            "exit_code": proc.returncode,
            "elapsed_ms": elapsed_ms,
            "stdout": stdout[:500],
            "stderr": stderr[:200],
            "error": None if exit_ok else f"exit code {proc.returncode} (expected {expect_exit})"
        }

        if exit_ok and output_contains:
            if output_contains not in stdout:
                result["ok"] = False
                result["error"] = f"output_contains '{output_contains}' not found in stdout"

        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s",
                "exit_code": None, "stdout": "", "stderr": ""}
    except Exception as e:
        return {"ok": False, "error": str(e), "exit_code": None, "stdout": "", "stderr": ""}


def check_process(watch: dict) -> dict:
    name     = watch.get("name")
    pid_file = watch.get("pid_file")
    require  = watch.get("require_running", True)

    found    = False
    pid      = None
    method   = None

    # pid file path takes priority
    if pid_file:
        pf = Path(pid_file)
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                # check if pid is alive
                os.kill(pid, 0)
                found  = True
                method = "pid_file"
            except (ProcessLookupError, PermissionError):
                # pid file exists but process is gone
                found  = False
                method = "pid_file"
            except Exception:
                found  = False
                method = "pid_file"
        else:
            found  = False
            method = "pid_file"

    # name-based search
    if not found and name:
        # try psutil
        try:
            import psutil
            for proc in psutil.process_iter(["name", "cmdline", "pid"]):
                try:
                    pname   = proc.info["name"] or ""
                    cmdline = " ".join(proc.info["cmdline"] or [])
                    if name.lower() in pname.lower() or name.lower() in cmdline.lower():
                        found  = True
                        pid    = proc.info["pid"]
                        method = "psutil"
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except ImportError:
            pass

        # fallback: pgrep
        if not found and method != "psutil":
            try:
                result = subprocess.run(
                    ["pgrep", "-f", name],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    pids  = result.stdout.strip().splitlines()
                    found = True
                    pid   = int(pids[0]) if pids else None
                    method = "pgrep"
                else:
                    method = "pgrep"
            except Exception:
                method = "fallback"

    ok = (found == require)
    err = None
    if not ok:
        if require and not found:
            err = f"process '{name or pid_file}' is not running"
        elif not require and found:
            err = f"process '{name or pid_file}' is running but should not be"

    return {
        "ok": ok,
        "found": found,
        "pid": pid,
        "method": method,
        "require_running": require,
        "error": err,
    }


def check_port(watch: dict) -> dict:
    import socket

    host    = watch.get("host", "localhost")
    port    = watch.get("port")
    timeout = watch.get("timeout_s", 10)

    if port is None:
        return {"ok": False, "error": "no port specified"}

    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"invalid port value: {watch.get('port')}"}

    start = time.monotonic()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result_code = sock.connect_ex((host, port))
        elapsed_ms = int((time.monotonic() - start) * 1000)
        sock.close()

        if result_code == 0:
            return {
                "ok": True,
                "host": host,
                "port": port,
                "elapsed_ms": elapsed_ms,
                "error": None,
            }
        else:
            return {
                "ok": False,
                "host": host,
                "port": port,
                "elapsed_ms": elapsed_ms,
                "error": f"connection refused or unreachable (connect_ex returned {result_code})",
            }
    except socket.timeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "host": host,
            "port": port,
            "elapsed_ms": elapsed_ms,
            "error": f"connection timed out after {timeout}s",
        }
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "host": host,
            "port": port,
            "elapsed_ms": elapsed_ms,
            "error": str(e),
        }


# ── dispatcher ─────────────────────────────────────────────────────────────────

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

def llm_evaluate(watch: dict, result: dict, history: list) -> dict:
    """
    ask the llm whether this result warrants an alert.
    returns {"alert": bool, "severity": str, "summary": str}
    gracefully returns a default if llm is unavailable.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # no llm available -- fall back to simple ok/not-ok
        alert = not result.get("ok", True)
        return {
            "alert": alert,
            "severity": "warning" if alert else "info",
            "summary": result.get("error") or ("ok" if not alert else "check failed"),
        }

    prompt = f"""You are a monitoring assistant. Given a watch definition and its current result, decide if an alert should be sent.

Watch definition:
{json.dumps(watch, indent=2)}

Current result:
{json.dumps(result, indent=2)}

Recent history (last {len(history)} checks):
{json.dumps(history, indent=2)}

Respond with ONLY valid JSON in this exact format:
{{"alert": true_or_false, "severity": "info|warning|critical", "summary": "one line summary"}}

Rules:
- alert: true if something is wrong or noteworthy
- alert: false if everything looks normal
- severity: info for minor/expected, warning for degraded, critical for down/broken
- summary: one concise line, no jargon
"""

    try:
        import urllib.request as urlreq
        payload = json.dumps({
            "model": "claude-3-haiku-20240307",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urlreq.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlreq.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())

        text = body["content"][0]["text"].strip()
        # pull json out even if there's surrounding text
        start = text.find("{")
        end   = text.rfind("}") + 1
        parsed = json.loads(text[start:end])
        return {
            "alert":    bool(parsed.get("alert", False)),
            "severity": parsed.get("severity", "info"),
            "summary":  parsed.get("summary", ""),
        }
    except Exception as e:
        alert = not result.get("ok", True)
        return {
            "alert":    alert,
            "severity": "warning" if alert else "info",
            "summary":  result.get("error") or "ok",
            "llm_error": str(e),
        }


# ── runner ─────────────────────────────────────────────────────────────────────

def run_all(watches: list, dry_run: bool = False, verbose: bool = False):
    alerts = []

    for watch in watches:
        wid   = watch.get("id", "?")
        wtype = watch.get("type", "?")
        label = watch.get("label", wid)

        if verbose:
            print(f"  checking [{wtype}] {label} ...", end=" ", flush=True)

        if dry_run:
            print(f"  [dry-run] would check [{wtype}] {label}")
            continue

        result  = run_check(watch)
        history = load_history(wid)
        eval_r  = llm_evaluate(watch, result, history)

        ts = int(time.time())
        log_entry = {
            "ts":       ts,
            "watch_id": wid,
            "type":     wtype,
            "label":    label,
            "result":   result,
            "eval":     eval_r,
            "alerted":  eval_r["alert"],
        }
        append_log(log_entry)

        if verbose:
            status = "ALERT" if eval_r["alert"] else "ok"
            print(f"{status} -- {eval_r['summary']}")

        if eval_r["alert"]:
            alerts.append((watch, result, eval_r))

    return alerts


def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true", help="show what would run, no checks")
    parser.add_argument("--id",       help="run only the watch with this id")
    parser.add_argument("--verbose",  action="store_true", help="print each check as it runs")
    args = parser.parse_args()

    watches = load_watches()
    if not watches:
        sys.exit(0)

    if args.id:
        watches = [w for w in watches if w.get("id") == args.id]
        if not watches:
            print(f"no watch found with id: {args.id}")
            sys.exit(1)

    if args.verbose or args.dry_run:
        print(f"watchdog: {len(watches)} watch(es) to run")

    alerts = run_all(watches, dry_run=args.dry_run, verbose=args.verbose)

    if args.dry_run:
        sys.exit(0)

    if not alerts:
        if args.verbose:
            print("all clear -- no alerts.")
        sys.exit(0)

    print(f"\nwatchdog: {len(alerts)} alert(s)\n")
    for watch, result, eval_r in alerts:
        severity = eval_r.get("severity", "warning").upper()
        label    = watch.get("label", watch.get("id", "?"))
        summary  = eval_r.get("summary", "")
        print(f"  [{severity}] {label}: {summary}")

    sys.exit(1)


if __name__ == "__main__":
    main()