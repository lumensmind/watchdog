#!/usr/bin/env python3
"""
watch_runner.py -- execute all active watchdog checks, evaluate with LLM, surface alerts.

usage:
    python3 scripts/watch_runner.py [--dry-run] [--id <watch-id>] [--verbose] [--since <minutes>] [--summary]
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

# -- paths ---------------------------------------------------------------------
SKILL_DIR    = Path(__file__).parent.parent
WATCHES_FILE = SKILL_DIR / "watches.json"
LOG_FILE     = SKILL_DIR / "watch_log.jsonl"
HISTORY_N    = 10   # last N results per watch for LLM context

# -- summary helpers -----------------------------------------------------------

STATUS_OK      = "OK"
STATUS_ALERT   = "ALERT"
STATUS_UNKNOWN = "UNKNOWN"

def format_summary_line(watch: dict, status: str, detail: str) -> str:
    """
    return a single formatted summary line for a watch.
    format: [STATUS] <id> (<type>) -- <detail>
    """
    watch_id   = watch.get("id", "?")
    watch_type = watch.get("type", "?")
    label      = watch.get("name", watch_id)
    # pad status for alignment
    padded = f"[{status}]".ljust(9)
    return f"{padded} {label} ({watch_type}) -- {detail}"


# -- main ----------------------------------------------------------------------

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


def condition_true_since(watch_id: str, since_minutes: float) -> bool:
    """
    return True if every log entry for this watch in the last since_minutes
    window shows alerted=True (i.e. the condition has been continuously firing).

    logic:
      1. look back through history for all entries within the since window.
      2. if there are no entries in the window, we have no evidence the condition
         has been true long enough -- suppress.
      3. if any entry in the window shows alerted=False (condition was clear),
         suppress -- the condition hasn't been continuously true.
      4. also require that the oldest entry in the window is at least
         since_minutes ago, so a single brand-new alert doesn't sneak through.
    """
    if not LOG_FILE.exists():
        return False

    now_ts = time.time()
    window_start = now_ts - (since_minutes * 60)

    entries_in_window = []
    with open(LOG_FILE) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except Exception:
                continue
            if entry.get("watch_id") != watch_id:
                continue
            ts = entry.get("ts", 0)
            if ts >= window_start:
                entries_in_window.append(entry)

    if not entries_in_window:
        return False

    # every entry in the window must show alerted=True
    if not all(e.get("alerted", False) for e in entries_in_window):
        return False

    # oldest entry in the window must be at least since_minutes old
    oldest_ts = min(e.get("ts", now_ts) for e in entries_in_window)
    if (now_ts - oldest_ts) < (since_minutes * 60):
        return False

    return True


# -- checkers ------------------------------------------------------------------

def check_http(watch: dict) -> dict:
    import urllib.request
    import urllib.error
    url        = watch.get("url", "")
    timeout    = watch.get("timeout", 10)
    expect     = watch.get("expect_status", 200)
    contains   = watch.get("contains")
    start      = time.time()
    try:
        req  = urllib.request.urlopen(url, timeout=timeout)
        code = req.getcode()
        body = req.read().decode("utf-8", errors="replace")
        elapsed = round((time.time() - start) * 1000)
        ok = (code == expect)
        detail = f"status={code} latency={elapsed}ms"
        if contains:
            if contains not in body:
                ok = False
                detail += f" missing_string='{contains}'"
        return {"ok": ok, "detail": detail, "raw": {"status": code, "latency_ms": elapsed}}
    except Exception as e:
        elapsed = round((time.time() - start) * 1000)
        return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}


def check_rss(watch: dict) -> dict:
    import urllib.request
    import xml.etree.ElementTree as ET
    url      = watch.get("url", "")
    keyword  = watch.get("keyword")
    max_age  = watch.get("max_age_hours")
    timeout  = watch.get("timeout", 10)
    try:
        req  = urllib.request.urlopen(url, timeout=timeout)
        body = req.read().decode("utf-8", errors="replace")
        root = ET.fromstring(body)
        ns   = {}
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        count = len(items)
        detail = f"items={count}"
        ok = True
        if keyword:
            matched = []
            for item in items:
                title = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or ""
                desc  = item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary") or ""
                if keyword.lower() in (title + desc).lower():
                    matched.append(title.strip())
            detail += f" keyword_matches={len(matched)}"
            if matched:
                detail += f" first='{matched[0][:60]}'"
        return {"ok": ok, "detail": detail, "raw": {"item_count": count}}
    except Exception as e:
        return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}


def check_disk(watch: dict) -> dict:
    path      = watch.get("path", "/")
    threshold = watch.get("threshold_pct", 90)
    try:
        import shutil
        usage = shutil.disk_usage(path)
        pct   = round(usage.used / usage.total * 100, 1)
        ok    = pct < threshold
        detail = f"used={pct}% threshold={threshold}% path={path}"
        return {"ok": ok, "detail": detail, "raw": {"used_pct": pct, "threshold_pct": threshold}}
    except Exception as e:
        return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}


def check_cpu(watch: dict) -> dict:
    threshold = watch.get("threshold_pct", 90)
    interval  = watch.get("interval_sec", 1)
    pct       = None
    try:
        import psutil
        pct = psutil.cpu_percent(interval=interval)
    except ImportError:
        # /proc/stat fallback
        try:
            def read_cpu():
                with open("/proc/stat") as f:
                    line = f.readline()
                fields = list(map(int, line.split()[1:]))
                idle   = fields[3]
                total  = sum(fields)
                return idle, total
            i1, t1 = read_cpu()
            time.sleep(interval)
            i2, t2 = read_cpu()
            pct = round((1 - (i2 - i1) / (t2 - t1)) * 100, 1)
        except Exception as e:
            return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}
    ok     = pct < threshold
    detail = f"cpu={pct}% threshold={threshold}%"
    return {"ok": ok, "detail": detail, "raw": {"cpu_pct": pct, "threshold_pct": threshold}}


def check_memory(watch: dict) -> dict:
    threshold = watch.get("threshold_pct", 90)
    pct       = None
    try:
        import psutil
        vm  = psutil.virtual_memory()
        pct = vm.percent
    except ImportError:
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    meminfo[k.strip()] = int(v.strip().split()[0])
            total     = meminfo["MemTotal"]
            available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            pct       = round((total - available) / total * 100, 1)
        except Exception as e:
            return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}
    ok     = pct < threshold
    detail = f"memory={pct}% threshold={threshold}%"
    return {"ok": ok, "detail": detail, "raw": {"memory_pct": pct, "threshold_pct": threshold}}


def check_file(watch: dict) -> dict:
    import os
    path       = watch.get("path", "")
    must_exist = watch.get("must_exist", True)
    max_age    = watch.get("max_age_minutes")
    grep       = watch.get("contains")

    if not os.path.exists(path):
        ok     = not must_exist
        detail = f"path={path} exists=False"
        return {"ok": ok, "detail": detail, "raw": {"exists": False}}

    detail = f"path={path} exists=True"
    ok     = True

    if max_age is not None:
        age_sec = time.time() - os.path.getmtime(path)
        age_min = round(age_sec / 60, 1)
        detail += f" age={age_min}m max={max_age}m"
        if age_min > max_age:
            ok = False

    if grep:
        try:
            with open(path, "r", errors="replace") as f:
                content = f.read()
            found = grep in content
            detail += f" contains='{grep}':{found}"
            if not found:
                ok = False
        except Exception as e:
            detail += f" read_error={e}"
            ok = False

    return {"ok": ok, "detail": detail, "raw": {"exists": True}}


def check_command(watch: dict) -> dict:
    cmd         = watch.get("command", "")
    expect_code = watch.get("expect_exit_code", 0)
    expect_out  = watch.get("expect_output_contains")
    timeout     = watch.get("timeout", 15)
    try:
        result  = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        ok      = result.returncode == expect_code
        detail  = f"exit={result.returncode}"
        if expect_out:
            found   = expect_out in result.stdout
            detail += f" output_contains='{expect_out}':{found}"
            if not found:
                ok = False
        return {"ok": ok, "detail": detail, "raw": {"exit_code": result.returncode, "stdout": result.stdout[:200]}}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"timeout after {timeout}s", "raw": {"error": "timeout"}}
    except Exception as e:
        return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}


def check_process(watch: dict) -> dict:
    name     = watch.get("name")
    pid_file = watch.get("pid_file")

    if pid_file:
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            exists = os.path.exists(f"/proc/{pid}")
            ok     = exists
            detail = f"pid_file={pid_file} pid={pid} running={exists}"
            return {"ok": ok, "detail": detail, "raw": {"pid": pid, "running": exists}}
        except Exception as e:
            return {"ok": False, "detail": f"pid_file_error={e}", "raw": {"error": str(e)}}

    if name:
        try:
            result = subprocess.run(
                ["pgrep", "-x", name], capture_output=True, text=True
            )
            running = result.returncode == 0
            pids    = result.stdout.strip().splitlines()
            detail  = f"process={name} running={running} pids={pids}"
            return {"ok": running, "detail": detail, "raw": {"name": name, "running": running, "pids": pids}}
        except Exception as e:
            # fallback: scan /proc
            try:
                found = []
                for entry in os.scandir("/proc"):
                    if not entry.name.isdigit():
                        continue
                    try:
                        comm = Path(f"/proc/{entry.name}/comm").read_text().strip()
                        if comm == name:
                            found.append(entry.name)
                    except Exception:
                        continue
                running = len(found) > 0
                detail  = f"process={name} running={running} pids={found}"
                return {"ok": running, "detail": detail, "raw": {"name": name, "running": running, "pids": found}}
            except Exception as e2:
                return {"ok": False, "detail": f"error={e2}", "raw": {"error": str(e2)}}

    return {"ok": False, "detail": "no name or pid_file specified", "raw": {}}


def check_port(watch: dict) -> dict:
    import socket
    host    = watch.get("host", "localhost")
    port    = watch.get("port")
    timeout = watch.get("timeout", 5)
    if port is None:
        return {"ok": False, "detail": "no port specified", "raw": {}}
    try:
        start = time.time()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        elapsed = round((time.time() - start) * 1000)
        detail  = f"host={host} port={port} open=True latency={elapsed}ms"
        return {"ok": True, "detail": detail, "raw": {"host": host, "port": port, "open": True, "latency_ms": elapsed}}
    except Exception as e:
        detail = f"host={host} port={port} open=False error={e}"
        return {"ok": False, "detail": detail, "raw": {"host": host, "port": port, "open": False, "error": str(e)}}


def check_ssl_cert(watch: dict) -> dict:
    import ssl
    import socket
    host      = watch.get("host", "")
    port      = watch.get("port", 443)
    threshold = watch.get("days_warning", 14)
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
            s.settimeout(watch.get("timeout", 10))
            s.connect((host, port))
            cert      = s.getpeercert()
        expire_str = cert["notAfter"]
        expire_dt  = datetime.strptime(expire_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_left  = (expire_dt - datetime.now(timezone.utc)).days
        ok         = days_left >= threshold
        detail     = f"host={host} days_left={days_left} threshold={threshold}"
        return {"ok": ok, "detail": detail, "raw": {"host": host, "days_left": days_left, "expires": expire_str}}
    except Exception as e:
        return {"ok": False, "detail": f"host={host} error={e}", "raw": {"host": host, "error": str(e)}}


def check_json_api(watch: dict) -> dict:
    import urllib.request
    import operator as op
    url       = watch.get("url", "")
    field     = watch.get("field", "")
    condition = watch.get("condition", "")
    timeout   = watch.get("timeout", 10)
    headers   = watch.get("headers", {})

    # condition format: "<operator> <value>"  e.g. "== ok" or "< 100"
    OPS = {
        "==": op.eq, "!=": op.ne,
        "<":  op.lt, "<=": op.le,
        ">":  op.gt, ">=": op.ge,
    }

    try:
        req_obj = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req_obj, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", errors="replace"))

        # dot-path field traversal
        value = body
        for part in field.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

        detail = f"field={field} value={value}"

        if condition:
            parts    = condition.strip().split(None, 1)
            cmp_op   = parts[0] if parts else ""
            cmp_val  = parts[1] if len(parts) > 1 else ""
            fn       = OPS.get(cmp_op)
            if fn is None:
                return {"ok": False, "detail": f"unknown operator '{cmp_op}'", "raw": {}}
            # coerce types
            try:
                typed_val = type(value)(cmp_val) if value is not None else cmp_val
            except Exception:
                typed_val = cmp_val
            ok      = fn(value, typed_val)
            detail += f" condition='{condition}' result={ok}"
        else:
            ok = value is not None

        return {"ok": ok, "detail": detail, "raw": {"field": field, "value": value}}
    except Exception as e:
        return {"ok": False, "detail": f"error={e}", "raw": {"error": str(e)}}


def check_ping(watch: dict) -> dict:
    host       = watch.get("host", "")
    count      = watch.get("count", 4)
    timeout    = watch.get("timeout", 5)
    max_loss   = watch.get("max_packet_loss_pct", 50)
    max_lat    = watch.get("max_latency_ms")

    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=(count * timeout) + 5
        )
        output = result.stdout + result.stderr

        # parse packet loss
        loss_pct = None
        for line in output.splitlines():
            if "packet loss" in line:
                for part in line.split(","):
                    if "packet loss" in part:
                        try:
                            loss_pct = float(part.strip().split("%")[0].split()[-1])
                        except Exception:
                            pass

        # parse avg latency from "rtt min/avg/max/mdev" line
        avg_ms = None
        for line in output.splitlines():
            if "rtt" in line and "avg" in line:
                try:
                    stats  = line.split("=")[1].strip().split("/")
                    avg_ms = float(stats[1])
                except Exception:
                    pass

        if loss_pct is None:
            return {"ok": False, "detail": f"host={host} could not parse ping output", "raw": {"output": output[:300]}}

        ok     = loss_pct <= max_loss
        detail = f"host={host} loss={loss_pct}% max_loss={max_loss}%"

        if avg_ms is not None:
            detail += f" avg_latency={avg_ms}ms"
            if max_lat is not None:
                detail += f" max_latency={max_lat}ms"
                if avg_ms > max_lat:
                    ok = False

        return {"ok": ok, "detail": detail, "raw": {"host": host, "loss_pct": loss_pct, "avg_ms": avg_ms}}

    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"host={host} ping timed out", "raw": {"host": host, "error": "timeout"}}
    except FileNotFoundError:
        return {"ok": False, "detail": f"ping command not found", "raw": {"error": "ping not found"}}
    except Exception as e:
        return {"ok": False, "detail": f"host={host} error={e}", "raw": {"error": str(e)}}


# -- llm eval ------------------------------------------------------------------

def llm_evaluate(watch: dict, check_result: dict, history: list) -> dict:
    """
    call anthropic to decide if this result warrants an alert.
    returns {"alerted": bool, "reason": str}
    gracefully degrades if no api key or import error.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        alerted = not check_result["ok"]
        return {"alerted": alerted, "reason": "no llm -- threshold only"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        history_summary = []
        for h in history[-5:]:
            history_summary.append({
                "ts":      h.get("ts"),
                "ok":      h.get("ok"),
                "detail":  h.get("detail"),
                "alerted": h.get("alerted"),
            })

        prompt = f"""you are a monitoring assistant. decide if this check result warrants an alert.

watch config:
{json.dumps(watch, indent=2)}

current result:
{json.dumps(check_result, indent=2)}

recent history (last {len(history_summary)} runs):
{json.dumps(history_summary, indent=2)}

rules:
- if ok=True and nothing looks anomalous, do NOT alert
- if ok=False, alert unless this is a known fluke (single blip with good history)
- use history to distinguish persistent problems from transient ones
- be concise

respond in json only:
{{"alerted": true|false, "reason": "<one sentence>"}}"""

        msg = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # strip markdown fences if present
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.split("```")[0]
        return json.loads(raw)
    except Exception as e:
        alerted = not check_result["ok"]
        return {"alerted": alerted, "reason": f"llm error ({e}) -- threshold only"}


# -- dispatch ------------------------------------------------------------------

CHECKERS = {
    "http":      check_http,
    "rss":       check_rss,
    "disk":      check_disk,
    "cpu":       check_cpu,
    "memory":    check_memory,
    "file":      check_file,
    "command":   check_command,
    "process":   check_process,
    "port":      check_port,
    "ssl_cert":  check_ssl_cert,
    "json_api":  check_json_api,
    "ping":      check_ping,
}


def run_watch(watch: dict, dry_run: bool, verbose: bool, since_minutes: Optional[float]) -> dict:
    """
    run a single watch. return the log entry dict.
    """
    watch_id   = watch.get("id", "unknown")
    watch_type = watch.get("type", "")
    ts         = time.time()

    checker = CHECKERS.get(watch_type)
    if not checker:
        result = {"ok": False, "detail": f"unknown watch type: {watch_type}", "raw": {}}
    else:
        try:
            result = checker(watch)
        except Exception as e:
            result = {"ok": False, "detail": f"checker exception: {e}", "raw": {"error": str(e)}}

    history = load_history(watch_id)

    if dry_run:
        eval_result = {"alerted": not result["ok"], "reason": "dry-run mode"}
    else:
        eval_result = llm_evaluate(watch, result, history)

    # apply --since suppression
    if since_minutes is not None and eval_result.get("alerted"):
        if not condition_true_since(watch_id, since_minutes):
            eval_result["alerted"]  = False
            eval_result["reason"] += f" (suppressed: condition not true for {since_minutes}m)"

    entry = {
        "watch_id": watch_id,
        "type":     watch_type,
        "ts":       ts,
        "ok":       result["ok"],
        "detail":   result["detail"],
        "alerted":  eval_result.get("alerted", False),
        "reason":   eval_result.get("reason", ""),
        "raw":      result.get("raw", {}),
    }

    if not dry_run:
        append_log(entry)

    return entry


def print_alert(entry: dict, watch: dict, verbose: bool):
    name = watch.get("name", entry["watch_id"])
    ts   = datetime.fromtimestamp(entry["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    if entry["alerted"]:
        print(f"[ALERT] {ts} | {name} | {entry['detail']}")
        print(f"        reason: {entry['reason']}")
    elif verbose:
        print(f"[ok]    {ts} | {name} | {entry['detail']}")


def print_summary_line(entry: dict, watch: dict):
    """
    print a single summary line for a watch result.
    used by --summary mode.
    """
    if entry.get("alerted"):
        status = STATUS_ALERT
    elif entry.get("ok"):
        status = STATUS_OK
    else:
        # check failed but llm suppressed the alert
        status = STATUS_ALERT

    detail = entry.get("detail", "no detail")
    line   = format_summary_line(watch, status, detail)
    print(line)


# -- entry point ---------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true", help="run checks but do not write logs or call llm")
    parser.add_argument("--id",       help="only run the watch with this id")
    parser.add_argument("--verbose",  action="store_true", help="print ok results too")
    parser.add_argument("--since",    type=float, metavar="MINUTES", help="only alert if condition has been true for at least N minutes")
    parser.add_argument("--summary",  action="store_true", help="print a one-line status for every watch (good for daily digests)")
    args = parser.parse_args()

    watches = load_watches()
    if not watches:
        sys.exit(1)

    if args.id:
        watches = [w for w in watches if w.get("id") == args.id]
        if not watches:
            print(f"no watch found with id '{args.id}'")
            sys.exit(1)

    if args.summary:
        # summary mode: run every watch, print one line each, no alert noise
        print(f"watchdog summary -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -- {len(watches)} watch(es)")
        print("-" * 72)
        alert_count = 0
        ok_count    = 0
        for watch in watches:
            entry = run_watch(watch, dry_run=args.dry_run, verbose=False, since_minutes=args.since)
            print_summary_line(entry, watch)
            if entry.get("alerted") or not entry.get("ok"):
                alert_count += 1
            else:
                ok_count += 1
        print("-" * 72)
        print(f"total: {ok_count} ok, {alert_count} alerting")
        return

    # normal mode
    alerted_count = 0
    for watch in watches:
        entry = run_watch(watch, dry_run=args.dry_run, verbose=args.verbose, since_minutes=args.since)
        print_alert(entry, watch, verbose=args.verbose)
        if entry.get("alerted"):
            alerted_count += 1

    if alerted_count:
        sys.exit(2)


if __name__ == "__main__":
    main()