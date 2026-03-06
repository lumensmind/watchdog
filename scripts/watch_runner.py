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
        return {"ok": False, "error": str(e), "entries": []}

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    items = root.findall(f".//{ns}item") or root.findall(f".//{ns}entry")
    entries = []
    for item in items[:10]:
        title = (item.findtext(f"{ns}title") or "").strip()
        link  = (item.findtext(f"{ns}link")  or "").strip()
        desc  = (item.findtext(f"{ns}description") or item.findtext(f"{ns}summary") or "").strip()
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
        "entries": entries[:5],
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
                "used_gb": round(used/1e9, 2), "total_gb": round(total/1e9, 2),
                "free_gb": round(free/1e9, 2), "warn_above_pct": warn_pct, "error": None}
    except Exception as e:
        return {"ok": False, "path": path, "error": str(e)}


def check_system_cpu(watch: dict) -> dict:
    warn_pct = watch.get("warn_above_pct", 90)

    # try psutil first
    try:
        import psutil
        pct = psutil.cpu_percent(interval=1)
        return {"ok": pct < warn_pct, "used_pct": pct, "warn_above_pct": warn_pct,
                "source": "psutil", "error": None}
    except ImportError:
        pass

    # fallback: read /proc/stat twice
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
        diff_total = total2 - total1
        diff_idle  = idle2  - idle1
        pct = round((1 - diff_idle / diff_total) * 100, 1) if diff_total else 0.0
        return {"ok": pct < warn_pct, "used_pct": pct, "warn_above_pct": warn_pct,
                "source": "/proc/stat", "error": None}
    except Exception as e:
        return {"ok": False, "used_pct": None, "error": str(e)}


def check_system_memory(watch: dict) -> dict:
    warn_pct = watch.get("warn_above_pct", 90)

    try:
        import psutil
        vm = psutil.virtual_memory()
        pct = vm.percent
        return {"ok": pct < warn_pct, "used_pct": pct, "warn_above_pct": warn_pct,
                "total_gb": round(vm.total/1e9, 2), "available_gb": round(vm.available/1e9, 2),
                "source": "psutil", "error": None}
    except ImportError:
        pass

    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":", 1)
                info[key.strip()] = int(val.split()[0])
        total     = info["MemTotal"]
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used      = total - available
        pct       = round(used / total * 100, 1)
        return {"ok": pct < warn_pct, "used_pct": pct, "warn_above_pct": warn_pct,
                "total_gb": round(total/1e6, 2), "available_gb": round(available/1e6, 2),
                "source": "/proc/meminfo", "error": None}
    except Exception as e:
        return {"ok": False, "used_pct": None, "error": str(e)}


def check_file(watch: dict) -> dict:
    path = watch.get("path")
    if not path:
        return {"ok": False, "error": "no path configured"}

    p = Path(path)
    if not p.exists():
        return {"ok": False, "exists": False, "error": f"file not found: {path}"}

    stat = p.stat()
    age_s = time.time() - stat.st_mtime
    result = {
        "ok": True,
        "exists": True,
        "size_bytes": stat.st_size,
        "age_s": int(age_s),
        "error": None,
    }

    max_age_s = watch.get("max_age_s")
    if max_age_s and age_s > max_age_s:
        result["ok"] = False
        result["error"] = f"file is {int(age_s)}s old, max allowed {max_age_s}s"

    grep = watch.get("contains")
    if grep:
        try:
            text = p.read_text(errors="replace")
            found = grep in text
            result["contains_match"] = found
            if not found:
                result["ok"] = False
                result["error"] = f"'{grep}' not found in file"
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)

    return result


def check_command(watch: dict) -> dict:
    cmd          = watch.get("command")
    expect_exit  = watch.get("expect_exit_code", 0)
    output_match = watch.get("output_contains")
    timeout      = watch.get("timeout_s", 30)

    if not cmd:
        return {"ok": False, "error": "no command configured"}

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        exit_ok = proc.returncode == expect_exit

        result = {
            "ok": exit_ok,
            "exit_code": proc.returncode,
            "stdout": stdout[:500],
            "stderr": stderr[:200],
            "error": None if exit_ok else f"exit code {proc.returncode}, expected {expect_exit}",
        }

        if exit_ok and output_match:
            if output_match not in stdout:
                result["ok"] = False
                result["error"] = f"output_contains '{output_match}' not found in stdout"
            result["output_match"] = output_match in stdout

        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "", "error": str(e)}


def check_process(watch: dict) -> dict:
    name     = watch.get("name")
    pid_file = watch.get("pid_file")

    if pid_file:
        pf = Path(pid_file)
        if not pf.exists():
            return {"ok": False, "error": f"pid file not found: {pid_file}", "source": "pid_file"}
        try:
            pid = int(pf.read_text().strip())
        except Exception as e:
            return {"ok": False, "error": f"could not read pid file: {e}", "source": "pid_file"}

        pid_path = Path(f"/proc/{pid}")
        if pid_path.exists():
            return {"ok": True, "pid": pid, "source": "pid_file", "error": None}

        # pid_file exists but process is gone -- try ps as fallback if name given
        if not name:
            return {"ok": False, "pid": pid, "error": f"process with pid {pid} not running", "source": "pid_file"}

    if not name:
        return {"ok": False, "error": "no name or pid_file configured"}

    # search by name via /proc or ps
    try:
        import psutil
        found = [p for p in psutil.process_iter(["name", "cmdline"])
                 if name.lower() in (p.info["name"] or "").lower()
                 or any(name.lower() in arg.lower() for arg in (p.info["cmdline"] or []))]
        if found:
            pids = [p.pid for p in found[:5]]
            return {"ok": True, "name": name, "pids": pids, "count": len(found),
                    "source": "psutil", "error": None}
        return {"ok": False, "name": name, "pids": [], "count": 0,
                "source": "psutil", "error": f"no process matching '{name}' found"}
    except ImportError:
        pass

    try:
        result = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
        pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
        if pids:
            return {"ok": True, "name": name, "pids": pids, "count": len(pids),
                    "source": "pgrep", "error": None}
        return {"ok": False, "name": name, "pids": [], "count": 0,
                "source": "pgrep", "error": f"no process matching '{name}' found"}
    except Exception as e:
        return {"ok": False, "name": name, "error": str(e)}


def check_port(watch: dict) -> dict:
    import socket

    host    = watch.get("host", "localhost")
    port    = watch.get("port")
    timeout = watch.get("timeout_s", 10)

    if port is None:
        return {"ok": False, "error": "no port configured"}

    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return {"ok": True, "host": host, "port": port,
                    "elapsed_ms": elapsed_ms, "error": None}
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {"ok": False, "host": host, "port": port,
                "elapsed_ms": elapsed_ms, "error": str(e)}


def check_ssl_cert(watch: dict) -> dict:
    import ssl
    import socket

    host    = watch.get("host")
    port    = watch.get("port", 443)
    timeout = watch.get("timeout_s", 10)
    warn_days = watch.get("warn_below_days", 14)

    if not host:
        return {"ok": False, "error": "no host configured"}

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
    except Exception as e:
        return {"ok": False, "host": host, "port": port, "error": str(e),
                "days_remaining": None, "expires": None}

    # cert["notAfter"] is a string like "Jan  1 00:00:00 2026 GMT"
    try:
        not_after_str = cert.get("notAfter", "")
        not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z")
        not_after = not_after.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        days_remaining = (not_after - now).days
    except Exception as e:
        return {"ok": False, "host": host, "port": port,
                "error": f"could not parse cert expiry: {e}",
                "days_remaining": None, "expires": not_after_str}

    ok = days_remaining >= warn_days
    return {
        "ok": ok,
        "host": host,
        "port": port,
        "expires": not_after_str,
        "days_remaining": days_remaining,
        "warn_below_days": warn_days,
        "error": None if ok else f"cert expires in {days_remaining} days (warn threshold: {warn_days})",
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
    "ssl_cert":      check_ssl_cert,
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
    call anthropic claude to evaluate the result.
    returns {"alert": bool, "severity": str, "summary": str}
    falls back to rule-based eval if api is unavailable.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return rule_based_eval(watch, result)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        history_snippet = json.dumps(history[-3:], indent=2) if history else "none"
        prompt = f"""you are a monitoring assistant evaluating a watch result.

watch definition:
{json.dumps(watch, indent=2)}

current result:
{json.dumps(result, indent=2)}

recent history (last few results):
{history_snippet}

based on the above, decide:
1. should an alert be raised? (true/false)
2. severity: info, warning, or critical
3. one-line summary of what is happening

respond with valid json only, no explanation:
{{"alert": true/false, "severity": "info|warning|critical", "summary": "..."}}"""

        msg = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # strip markdown code fences if present
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines()
                if not line.startswith("```")
            ).strip()
        return json.loads(text)
    except Exception as e:
        return rule_based_eval(watch, result)


def rule_based_eval(watch: dict, result: dict) -> dict:
    ok = result.get("ok", False)
    if ok:
        return {"alert": False, "severity": "info", "summary": "ok"}
    err = result.get("error") or "check failed"
    return {"alert": True, "severity": "warning", "summary": err}


# ── runner ─────────────────────────────────────────────────────────────────────

def run_all(watches: list, dry_run: bool = False, verbose: bool = False, filter_id: Optional[str] = None):
    alerts = []

    for watch in watches:
        wid   = watch.get("id", "unknown")
        wtype = watch.get("type", "unknown")
        name  = watch.get("name", wid)

        if filter_id and wid != filter_id:
            continue

        if dry_run:
            print(f"[dry-run] would check: {name} ({wtype})")
            continue

        if verbose:
            print(f"checking: {name} ({wtype}) ...", end=" ", flush=True)

        result  = run_check(watch)
        history = load_history(wid)
        eval_r  = llm_evaluate(watch, result, history)

        ts = int(time.time())
        log_entry = {
            "ts":       ts,
            "watch_id": wid,
            "type":     wtype,
            "result":   result,
            "eval":     eval_r,
            "alerted":  eval_r.get("alert", False),
        }
        append_log(log_entry)

        if verbose:
            status = "ALERT" if eval_r.get("alert") else "ok"
            print(f"{status} -- {eval_r.get('summary', '')}")

        if eval_r.get("alert"):
            alerts.append({"watch": watch, "result": result, "eval": eval_r})

    return alerts


def print_alerts(alerts: list):
    if not alerts:
        print("watchdog: all clear")
        return

    print(f"\nwatchdog: {len(alerts)} alert(s)\n")
    for a in alerts:
        w    = a["watch"]
        ev   = a["eval"]
        res  = a["result"]
        sev  = ev.get("severity", "warning").upper()
        name = w.get("name", w.get("id", "?"))
        print(f"  [{sev}] {name}")
        print(f"    {ev.get('summary', '')}")
        if res.get("error"):
            print(f"    error: {res['error']}")
        print()


def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true", help="show what would run, no checks")
    parser.add_argument("--id",       metavar="WATCH_ID",  help="only run this watch id")
    parser.add_argument("--verbose",  action="store_true", help="print each check as it runs")
    args = parser.parse_args()

    watches = load_watches()
    if not watches:
        sys.exit(0)

    alerts = run_all(watches, dry_run=args.dry_run, verbose=args.verbose, filter_id=args.id)

    if not args.dry_run:
        print_alerts(alerts)

    sys.exit(1 if alerts else 0)


if __name__ == "__main__":
    main()