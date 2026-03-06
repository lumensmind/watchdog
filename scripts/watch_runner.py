#!/usr/bin/env python3
"""
watch_runner.py -- execute all active watchdog checks, evaluate with LLM, surface alerts.

usage:
    python3 scripts/watch_runner.py [--dry-run] [--id <watch-id>] [--verbose] [--since <minutes>]
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

    # every entry in the window must have been an alert
    all_alerting = all(e.get("alerted", False) for e in entries_in_window)
    if not all_alerting:
        return False

    # the earliest alerting entry in the window must be old enough
    oldest_ts = min(e.get("ts", now_ts) for e in entries_in_window)
    if oldest_ts > window_start:
        # oldest entry is newer than the window start -- condition hasn't been
        # confirmed for the full duration yet
        return False

    return True


# -- checkers ------------------------------------------------------------------

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
        "entry_count": len(entries),
        "entries": entries[:5],
        "keyword": keyword,
        "keyword_matches": matched if keyword else None,
        "error": None,
    }


def check_system_disk(watch: dict) -> dict:
    path      = watch.get("path", "/")
    threshold = watch.get("threshold_pct", 90)
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        pct = round(used / total * 100, 1)
        return {"ok": pct < threshold, "used_pct": pct, "threshold_pct": threshold,
                "path": path, "free_gb": round(free / 1e9, 2), "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_system_cpu(watch: dict) -> dict:
    threshold = watch.get("threshold_pct", 90)
    try:
        import psutil
        pct = psutil.cpu_percent(interval=2)
    except ImportError:
        try:
            def _read_cpu():
                with open("/proc/stat") as f:
                    parts = f.readline().split()
                idle, total = int(parts[4]), sum(int(x) for x in parts[1:])
                return idle, total
            i1, t1 = _read_cpu(); time.sleep(2); i2, t2 = _read_cpu()
            pct = round((1 - (i2 - i1) / (t2 - t1)) * 100, 1)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": pct < threshold, "cpu_pct": pct, "threshold_pct": threshold, "error": None}


def check_system_memory(watch: dict) -> dict:
    threshold = watch.get("threshold_pct", 90)
    try:
        import psutil
        vm  = psutil.virtual_memory()
        pct = vm.percent
    except ImportError:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":")
                    info[k.strip()] = int(v.strip().split()[0])
            total = info["MemTotal"]
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            pct   = round((1 - avail / total) * 100, 1)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": pct < threshold, "mem_pct": pct, "threshold_pct": threshold, "error": None}


def check_file(watch: dict) -> dict:
    import hashlib
    path = Path(watch["path"])
    checks = watch.get("checks", ["exists"])
    result = {"path": str(path), "ok": True, "error": None}

    if not path.exists():
        result["ok"] = "exists" not in checks
        result["exists"] = False
        if "exists" in checks:
            result["error"] = "file does not exist"
        return result

    result["exists"] = True
    stat = path.stat()
    result["size_bytes"] = stat.st_size
    result["mtime"]      = int(stat.st_mtime)
    age_minutes = (time.time() - stat.st_mtime) / 60
    result["age_minutes"] = round(age_minutes, 1)

    if "max_age_minutes" in watch:
        if age_minutes > watch["max_age_minutes"]:
            result["ok"]    = False
            result["error"] = f"file is {round(age_minutes,1)} min old, max is {watch['max_age_minutes']}"

    if "min_size_bytes" in watch:
        if stat.st_size < watch["min_size_bytes"]:
            result["ok"]    = False
            result["error"] = f"file is {stat.st_size} bytes, min is {watch['min_size_bytes']}"

    if "grep" in watch:
        try:
            content = path.read_text(errors="replace")
            found   = watch["grep"] in content
            result["grep_match"] = found
            if not found:
                result["ok"]    = False
                result["error"] = f"grep '{watch['grep']}' not found in file"
        except Exception as e:
            result["ok"]    = False
            result["error"] = str(e)

    if "hash_sha256" in watch:
        try:
            h = hashlib.sha256(path.read_bytes()).hexdigest()
            result["hash_sha256"] = h
            if h != watch["hash_sha256"]:
                result["ok"]    = False
                result["error"] = "file hash mismatch"
        except Exception as e:
            result["ok"]    = False
            result["error"] = str(e)

    return result


def check_command(watch: dict) -> dict:
    cmd     = watch["command"]
    timeout = watch.get("timeout_s", 30)
    expect_exit   = watch.get("expect_exit", 0)
    expect_output = watch.get("expect_output")

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        exit_ok = proc.returncode == expect_exit
        output_ok = True
        if expect_output and expect_output not in stdout:
            output_ok = False
        ok = exit_ok and output_ok
        return {
            "ok": ok,
            "exit_code": proc.returncode,
            "stdout": stdout[:500],
            "stderr": stderr[:200],
            "error": None if ok else f"exit={proc.returncode}, expected={expect_exit}" +
                     (f", output mismatch" if not output_ok else ""),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "", "error": str(e)}


def check_process(watch: dict) -> dict:
    name     = watch.get("name")
    pid_file = watch.get("pid_file")
    result   = {"ok": False, "error": None}

    if pid_file:
        pid_path = Path(pid_file)
        if not pid_path.exists():
            result["error"] = f"pid file not found: {pid_file}"
            return result
        try:
            pid = int(pid_path.read_text().strip())
            result["pid"] = pid
            proc_path = Path(f"/proc/{pid}")
            if proc_path.exists():
                result["ok"] = True
            else:
                result["error"] = f"pid {pid} from pid file is not running"
        except Exception as e:
            result["error"] = str(e)
        return result

    if name:
        try:
            import psutil
            found = [p for p in psutil.process_iter(["name", "cmdline"])
                     if name.lower() in (p.info["name"] or "").lower()
                     or any(name.lower() in (a or "").lower() for a in (p.info["cmdline"] or []))]
            result["ok"]    = len(found) > 0
            result["count"] = len(found)
            if not result["ok"]:
                result["error"] = f"no process matching '{name}' found"
        except ImportError:
            try:
                out = subprocess.check_output(["pgrep", "-f", name], text=True)
                pids = [p.strip() for p in out.strip().splitlines() if p.strip()]
                result["ok"]    = len(pids) > 0
                result["count"] = len(pids)
                if not result["ok"]:
                    result["error"] = f"no process matching '{name}' found"
            except subprocess.CalledProcessError:
                result["ok"]    = False
                result["count"] = 0
                result["error"] = f"no process matching '{name}' found"
            except Exception as e:
                result["error"] = str(e)
        return result

    result["error"] = "process watch requires 'name' or 'pid_file'"
    return result


def check_port(watch: dict) -> dict:
    import socket
    host    = watch.get("host", "localhost")
    port    = watch["port"]
    timeout = watch.get("timeout_s", 5)
    start   = time.monotonic()
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
    host        = watch["host"]
    port        = watch.get("port", 443)
    warn_days   = watch.get("warn_days", 14)
    timeout     = watch.get("timeout_s", 10)
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((host, port), timeout=timeout),
                             server_hostname=host) as ssock:
            cert = ssock.getpeercert()
        expires_str = cert["notAfter"]
        expires_dt  = datetime.strptime(expires_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_left   = (expires_dt - datetime.now(timezone.utc)).days
        ok          = days_left >= warn_days
        return {"ok": ok, "host": host, "port": port,
                "days_until_expiry": days_left, "expires": expires_str,
                "warn_days": warn_days,
                "error": None if ok else f"cert expires in {days_left} days (warn threshold: {warn_days})"}
    except Exception as e:
        return {"ok": False, "host": host, "port": port, "error": str(e)}


def check_json_api(watch: dict) -> dict:
    import urllib.request

    url       = watch["url"]
    field     = watch.get("field")
    condition = watch.get("condition")
    timeout   = watch.get("timeout_s", 10)

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except Exception as e:
        return {"ok": False, "error": str(e), "field": field, "value": None}

    value = None
    if field:
        try:
            keys  = field.split(".")
            value = data
            for k in keys:
                if isinstance(value, list):
                    value = value[int(k)]
                else:
                    value = value[k]
        except Exception as e:
            return {"ok": False, "error": f"could not extract field '{field}': {e}",
                    "field": field, "value": None, "raw_sample": str(data)[:300]}

    ok    = True
    error = None
    if condition and value is not None:
        cond = condition.strip()
        try:
            ok = bool(eval(cond, {"__builtins__": {}}, {"value": value, "v": value}))
            if not ok:
                error = f"condition '{cond}' failed for value {repr(value)}"
        except Exception as e:
            ok    = False
            error = f"could not evaluate condition '{cond}': {e}"

    return {"ok": ok, "field": field, "value": value, "condition": condition,
            "error": error, "raw_sample": str(data)[:300]}


def check_ping(watch: dict) -> dict:
    host      = watch["host"]
    count     = watch.get("count", 4)
    timeout   = watch.get("timeout_s", 5)
    max_loss  = watch.get("max_loss_pct", 50)
    max_ms    = watch.get("max_latency_ms")

    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=timeout * count + 5
        )
        output   = result.stdout + result.stderr
        loss_pct = None
        avg_ms   = None

        for line in output.splitlines():
            if "packet loss" in line:
                for part in line.split(","):
                    if "packet loss" in part:
                        try:
                            loss_pct = float(part.strip().split("%")[0].split()[-1])
                        except Exception:
                            pass
            if "rtt" in line or "round-trip" in line:
                try:
                    stats  = line.split("=")[-1].strip().split("/")
                    avg_ms = float(stats[1])
                except Exception:
                    pass

        ok    = True
        error = None
        if loss_pct is not None and loss_pct > max_loss:
            ok    = False
            error = f"packet loss {loss_pct}% exceeds max {max_loss}%"
        if max_ms and avg_ms is not None and avg_ms > max_ms:
            ok    = False
            error = (error or "") + f" latency {avg_ms}ms exceeds max {max_ms}ms"

        return {"ok": ok, "host": host, "loss_pct": loss_pct, "avg_ms": avg_ms,
                "error": error, "raw": output[-300:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "host": host, "loss_pct": 100, "avg_ms": None,
                "error": "ping timed out"}
    except Exception as e:
        return {"ok": False, "host": host, "loss_pct": None, "avg_ms": None, "error": str(e)}


# -- dispatch ------------------------------------------------------------------

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


# -- llm eval ------------------------------------------------------------------

def llm_evaluate(watch: dict, result: dict, history: list) -> dict:
    """
    call the anthropic api to evaluate if this result warrants an alert.
    returns dict with keys: alert (bool), severity (str), summary (str).
    falls back gracefully if no api key or import fails.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # fallback: alert on any not-ok result
        return {
            "alert":    not result.get("ok", True),
            "severity": "warning",
            "summary":  result.get("error") or ("check failed" if not result.get("ok") else "ok"),
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        history_summary = []
        for h in history[-5:]:
            history_summary.append({
                "ts":      h.get("ts"),
                "ok":      h.get("result", {}).get("ok"),
                "alerted": h.get("alerted"),
                "summary": h.get("llm", {}).get("summary"),
            })

        prompt = f"""You are a monitoring assistant. Evaluate this watch result and decide if it warrants an alert.

Watch definition:
{json.dumps(watch, indent=2)}

Current result:
{json.dumps(result, indent=2)}

Recent history (last {len(history_summary)} runs):
{json.dumps(history_summary, indent=2)}

Respond with a JSON object only, no markdown, no explanation:
{{"alert": true or false, "severity": "info" or "warning" or "critical", "summary": "one line explanation"}}

Rules:
- alert: true only if something is actually wrong or noteworthy
- do not alert if result is ok and history shows it has been consistently ok
- severity: info for minor/expected issues, warning for degraded, critical for down/data loss risk
- summary: be concise and specific, mention the actual value if relevant"""

        message = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        return json.loads(raw)

    except Exception as e:
        return {
            "alert":    not result.get("ok", True),
            "severity": "warning",
            "summary":  result.get("error") or f"llm eval failed: {e}",
        }


# -- entry point ---------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true",
                        help="collect results but do not log or alert")
    parser.add_argument("--id",       metavar="WATCH_ID",
                        help="only run the watch with this id")
    parser.add_argument("--verbose",  action="store_true",
                        help="print all results, not just alerts")
    parser.add_argument("--since",    metavar="MINUTES", type=float,
                        help="only alert if the condition has been true for at least N minutes (reduces noise)")
    args = parser.parse_args()

    watches = load_watches()
    if not watches:
        sys.exit(0)

    if args.id:
        watches = [w for w in watches if w.get("id") == args.id]
        if not watches:
            print(f"no watch found with id '{args.id}'")
            sys.exit(1)

    alerts = []

    for watch in watches:
        watch_id = watch.get("id", watch.get("name", "unknown"))
        wtype    = watch.get("type", "unknown")

        if args.verbose:
            print(f"checking [{watch_id}] ({wtype}) ...")

        result  = run_check(watch)
        history = load_history(watch_id)
        llm     = llm_evaluate(watch, result, history)

        # --since gate: if the flag is set, only let an alert through if the
        # condition has been continuously firing for at least that many minutes.
        # we check history BEFORE appending this run, so the current run is
        # not yet in the log when we evaluate.
        suppressed_by_since = False
        if args.since is not None and llm.get("alert"):
            if not condition_true_since(watch_id, args.since):
                suppressed_by_since = True
                if args.verbose:
                    print(f"  [{watch_id}] alert suppressed by --since {args.since}m "
                          f"(condition not confirmed for full duration yet)")

        alerted = llm.get("alert", False) and not suppressed_by_since

        log_entry = {
            "ts":       int(time.time()),
            "watch_id": watch_id,
            "type":     wtype,
            "result":   result,
            "llm":      llm,
            "alerted":  alerted,
        }

        if args.since is not None:
            log_entry["since_minutes"] = args.since
            log_entry["suppressed_by_since"] = suppressed_by_since

        if not args.dry_run:
            append_log(log_entry)

        if alerted:
            alerts.append((watch, result, llm))
            severity = llm.get("severity", "warning").upper()
            summary  = llm.get("summary", "")
            print(f"[{severity}] {watch_id}: {summary}")
        elif args.verbose:
            summary = llm.get("summary", "")
            tag     = "SUPPRESSED" if suppressed_by_since else "ok"
            print(f"  [{watch_id}] {tag}: {summary}")

    if args.dry_run:
        print("[dry-run] no alerts sent, no log written")

    if not alerts and not args.verbose and not args.dry_run:
        pass  # silent success -- nothing to report

    return len(alerts)


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result == 0 else 1)