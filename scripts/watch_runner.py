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
    timeout = watch.get("timeout_s", 10)

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)

        # find all items (rss) or entries (atom)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        entries = []
        for item in items[:10]:
            title = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
            link  = (item.findtext("link")  or item.findtext("atom:link",  namespaces=ns) or "").strip()
            pub   = (item.findtext("pubDate") or item.findtext("atom:published", namespaces=ns) or "").strip()
            entries.append({"title": title, "link": link, "pub": pub})

        keyword_hits = []
        if keyword:
            for e in entries:
                if keyword.lower() in e["title"].lower():
                    keyword_hits.append(e)

        return {
            "ok": True,
            "entry_count": len(entries),
            "latest": entries[0] if entries else None,
            "keyword_hits": keyword_hits,
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "entry_count": 0, "latest": None, "keyword_hits": [], "error": str(e)}


def check_system_disk(watch: dict) -> dict:
    import shutil
    path = watch.get("path", "/")
    threshold = watch.get("alert_threshold_pct", 90)
    try:
        usage = shutil.disk_usage(path)
        pct = round(usage.used / usage.total * 100, 1)
        return {
            "ok": pct < threshold,
            "used_pct": pct,
            "used_gb": round(usage.used / 1e9, 2),
            "total_gb": round(usage.total / 1e9, 2),
            "threshold_pct": threshold,
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_system_cpu(watch: dict) -> dict:
    threshold = watch.get("alert_threshold_pct", 90)
    try:
        import psutil
        pct = psutil.cpu_percent(interval=1)
        return {"ok": pct < threshold, "cpu_pct": pct, "threshold_pct": threshold, "error": None}
    except ImportError:
        # fallback: read /proc/stat
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            fields = list(map(int, line.split()[1:]))
            idle = fields[3]
            total = sum(fields)
            pct = round((1 - idle / total) * 100, 1)
            return {"ok": pct < threshold, "cpu_pct": pct, "threshold_pct": threshold, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def check_system_memory(watch: dict) -> dict:
    threshold = watch.get("alert_threshold_pct", 90)
    try:
        import psutil
        mem = psutil.virtual_memory()
        pct = mem.percent
        return {"ok": pct < threshold, "mem_pct": pct, "used_gb": round(mem.used / 1e9, 2),
                "total_gb": round(mem.total / 1e9, 2), "threshold_pct": threshold, "error": None}
    except ImportError:
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for l in lines:
                k, v = l.split(":", 1)
                info[k.strip()] = int(v.strip().split()[0])
            total = info.get("MemTotal", 1)
            avail = info.get("MemAvailable", total)
            pct = round((1 - avail / total) * 100, 1)
            return {"ok": pct < threshold, "mem_pct": pct, "threshold_pct": threshold, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def check_file(watch: dict) -> dict:
    import hashlib
    path = Path(watch["path"])
    expect_exists = watch.get("expect_exists", True)
    max_age_s     = watch.get("max_age_s")
    grep          = watch.get("contains")

    if not path.exists():
        return {"ok": not expect_exists, "exists": False,
                "error": "file not found" if expect_exists else None}

    stat = path.stat()
    age_s = time.time() - stat.st_mtime
    result = {"ok": True, "exists": True, "size_bytes": stat.st_size,
              "age_s": round(age_s), "error": None}

    if max_age_s and age_s > max_age_s:
        result["ok"] = False
        result["error"] = f"file not modified in {round(age_s)}s (max {max_age_s}s)"

    if grep:
        try:
            content = path.read_text(errors="replace")
            found = grep in content
            result["grep_found"] = found
            if not found:
                result["ok"] = False
                result["error"] = f"'{grep}' not found in file"
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)

    return result


def check_command(watch: dict) -> dict:
    cmd          = watch["command"]
    expect_exit  = watch.get("expect_exit", 0)
    output_match = watch.get("output_contains")
    timeout      = watch.get("timeout_s", 15)

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        stdout = proc.stdout.strip()[:500]
        ok = (proc.returncode == expect_exit)
        if ok and output_match and output_match not in stdout:
            ok = False
        return {"ok": ok, "exit_code": proc.returncode, "stdout": stdout,
                "expected_exit": expect_exit, "error": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


CHECKERS = {
    "http":          check_http,
    "rss":           check_rss,
    "system_disk":   check_system_disk,
    "system_cpu":    check_system_cpu,
    "system_memory": check_system_memory,
    "file":          check_file,
    "command":       check_command,
}


# ── llm evaluation ─────────────────────────────────────────────────────────────

def evaluate_with_llm(watch: dict, result: dict, history: list) -> dict:
    """Ask the LLM if this result is worth alerting on. Returns {alert, severity, summary}."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        # no llm available -- fall back to simple ok/not-ok
        alerted = not result.get("ok", True)
        return {
            "alert": alerted,
            "severity": "warning" if alerted else "info",
            "summary": result.get("error") or ("check failed" if alerted else "ok"),
        }

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        history_str = ""
        if history:
            recent = history[-5:]
            history_str = "\nrecent history (last {} checks):\n".format(len(recent))
            for h in recent:
                history_str += f"  {h.get('ts_iso', '')} -- ok={h['result'].get('ok')} alerted={h.get('alerted')}\n"

        prompt = (
            f"watch: {watch.get('name', watch['id'])} (type: {watch['type']})\n"
            f"config: {json.dumps({k: v for k, v in watch.items() if k not in ('id','type','name','enabled')})}\n"
            f"current result: {json.dumps(result)}\n"
            f"{history_str}\n"
            "is this result worth alerting the user about?\n"
            "respond with json only, no explanation:\n"
            '{"alert": true/false, "severity": "info"|"warning"|"critical", "summary": "<one line>"}'
        )

        msg = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # parse json out of response (may have backticks)
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        return json.loads(text)
    except Exception as e:
        alerted = not result.get("ok", True)
        return {
            "alert": alerted,
            "severity": "warning" if alerted else "info",
            "summary": f"llm eval failed ({e}); raw ok={result.get('ok')}",
        }


# ── runner ─────────────────────────────────────────────────────────────────────

def run_watch(watch: dict, dry_run: bool = False, verbose: bool = False) -> dict:
    wtype   = watch.get("type")
    checker = CHECKERS.get(wtype)
    if not checker:
        print(f"  [skip] unknown watch type '{wtype}' for watch '{watch['id']}'")
        return None

    if verbose:
        print(f"  checking: {watch.get('name', watch['id'])} ({wtype})")

    result = checker(watch)
    history = load_history(watch["id"])

    if dry_run:
        print(f"    result: {json.dumps(result)}")
        return None

    evaluation = evaluate_with_llm(watch, result, history)

    ts = int(time.time())
    entry = {
        "ts":        ts,
        "ts_iso":    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "watch_id":  watch["id"],
        "type":      wtype,
        "result":    result,
        "alerted":   evaluation["alert"],
        "severity":  evaluation["severity"],
        "summary":   evaluation["summary"],
    }
    append_log(entry)

    if evaluation["alert"]:
        print(f"  [ALERT] {watch.get('name', watch['id'])}: {evaluation['summary']}")

    return entry


def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true", help="check but do not log or alert")
    parser.add_argument("--id",       help="run only this watch id")
    parser.add_argument("--verbose",  action="store_true", help="print each check")
    parser.add_argument("--json",     action="store_true", help="output results as json")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(SKILL_DIR / ".env", override=False)
    load_dotenv(override=False)  # also try cwd .env

    watches = load_watches()
    if not watches:
        sys.exit(0)

    if args.id:
        watches = [w for w in watches if w["id"] == args.id]
        if not watches:
            print(f"no watch with id '{args.id}'")
            sys.exit(1)

    alerts = []
    for watch in watches:
        entry = run_watch(watch, dry_run=args.dry_run, verbose=args.verbose or bool(args.id))
        if entry and entry.get("alerted"):
            alerts.append(entry)

    if args.json:
        print(json.dumps({"alerts": alerts, "total_watches": len(watches)}))
    elif alerts:
        print(f"\n{len(alerts)} alert(s) from {len(watches)} watch(es)")
    else:
        print(f"all clear -- {len(watches)} watch(es) checked")


if __name__ == "__main__":
    main()
