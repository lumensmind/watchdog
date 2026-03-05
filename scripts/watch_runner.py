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
        return {"ok": False, "error": str(e), "new_entries": [], "matched": []}

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # support both rss <item> and atom <entry>
    items = root.findall(f".//{ns}item") or root.findall(f".//{ns}entry")

    entries = []
    for item in items[:20]:
        title = (item.findtext(f"{ns}title") or "").strip()
        link  = (item.findtext(f"{ns}link") or "").strip()
        pub   = (item.findtext(f"{ns}pubDate") or item.findtext(f"{ns}updated") or "").strip()
        desc  = (item.findtext(f"{ns}description") or item.findtext(f"{ns}summary") or "").strip()
        entries.append({"title": title, "link": link, "published": pub, "description": desc[:200]})

    matched = []
    if keyword:
        kw = keyword.lower()
        for e in entries:
            if kw in e["title"].lower() or kw in e["description"].lower():
                matched.append(e)

    return {
        "ok": True,
        "error": None,
        "entry_count": len(entries),
        "entries": entries[:5],
        "keyword": keyword,
        "matched": matched,
    }


def check_system_disk(watch: dict) -> dict:
    path  = watch.get("path", "/")
    warn  = watch.get("warn_percent", 80)
    crit  = watch.get("critical_percent", 90)

    try:
        import psutil
        usage = psutil.disk_usage(path)
        pct   = usage.percent
    except ImportError:
        # fallback via df
        try:
            out = subprocess.check_output(["df", "-h", path], text=True)
            line = out.strip().splitlines()[-1]
            pct_str = [x for x in line.split() if x.endswith("%")]
            if not pct_str:
                return {"ok": False, "error": "could not parse df output"}
            pct = float(pct_str[0].rstrip("%"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    ok = pct < crit
    return {
        "ok": ok,
        "path": path,
        "used_percent": pct,
        "warn_percent": warn,
        "critical_percent": crit,
        "error": None if ok else f"disk usage {pct:.1f}% exceeds critical threshold {crit}%",
    }


def check_system_cpu(watch: dict) -> dict:
    warn = watch.get("warn_percent", 80)
    crit = watch.get("critical_percent", 95)

    try:
        import psutil
        pct = psutil.cpu_percent(interval=1)
    except ImportError:
        # fallback via /proc/stat -- two samples 1s apart
        def _read_proc_stat():
            with open("/proc/stat") as f:
                line = f.readline()
            fields = list(map(int, line.split()[1:]))
            idle  = fields[3]
            total = sum(fields)
            return idle, total

        try:
            idle1, total1 = _read_proc_stat()
            time.sleep(1)
            idle2, total2 = _read_proc_stat()
            delta_total = total2 - total1
            delta_idle  = idle2  - idle1
            pct = 100.0 * (1 - delta_idle / delta_total) if delta_total else 0.0
        except Exception as e:
            return {"ok": False, "error": str(e)}

    ok = pct < crit
    return {
        "ok": ok,
        "cpu_percent": round(pct, 1),
        "warn_percent": warn,
        "critical_percent": crit,
        "error": None if ok else f"cpu usage {pct:.1f}% exceeds critical threshold {crit}%",
    }


def check_system_memory(watch: dict) -> dict:
    warn = watch.get("warn_percent", 80)
    crit = watch.get("critical_percent", 95)

    try:
        import psutil
        mem = psutil.virtual_memory()
        pct = mem.percent
    except ImportError:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, val = line.split(":")
                    info[key.strip()] = int(val.strip().split()[0])
            total     = info["MemTotal"]
            available = info.get("MemAvailable", info.get("MemFree", 0))
            pct = 100.0 * (1 - available / total) if total else 0.0
        except Exception as e:
            return {"ok": False, "error": str(e)}

    ok = pct < crit
    return {
        "ok": ok,
        "used_percent": round(pct, 1),
        "warn_percent": warn,
        "critical_percent": crit,
        "error": None if ok else f"memory usage {pct:.1f}% exceeds critical threshold {crit}%",
    }


def check_file(watch: dict) -> dict:
    path = watch.get("path")
    if not path:
        return {"ok": False, "error": "no path specified"}

    p = Path(path)
    if not p.exists():
        return {"ok": False, "exists": False, "error": f"file not found: {path}"}

    stat = p.stat()
    age_s = time.time() - stat.st_mtime
    result = {
        "ok": True,
        "exists": True,
        "size_bytes": stat.st_size,
        "age_seconds": int(age_s),
        "error": None,
    }

    max_age_s = watch.get("max_age_seconds")
    if max_age_s and age_s > max_age_s:
        result["ok"] = False
        result["error"] = f"file is {int(age_s)}s old, max allowed is {max_age_s}s"

    grep = watch.get("grep")
    if grep:
        try:
            content = p.read_text(errors="replace")
            found = grep in content
            result["grep"] = grep
            result["grep_found"] = found
            if not found:
                result["ok"] = False
                result["error"] = f"grep '{grep}' not found in file"
        except Exception as e:
            result["ok"] = False
            result["error"] = f"could not read file: {e}"

    return result


def check_command(watch: dict) -> dict:
    cmd           = watch.get("command")
    expect_exit   = watch.get("expect_exit_code", 0)
    stdout_match  = watch.get("stdout_contains")
    timeout       = watch.get("timeout_s", 30)

    if not cmd:
        return {"ok": False, "error": "no command specified"}

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        exit_code = proc.returncode
        stdout    = proc.stdout.strip()
        stderr    = proc.stderr.strip()

        ok = (exit_code == expect_exit)
        error = None if ok else f"exit code {exit_code}, expected {expect_exit}"

        if ok and stdout_match and stdout_match not in stdout:
            ok    = False
            error = f"stdout_contains '{stdout_match}' not found"

        return {
            "ok": ok,
            "exit_code": exit_code,
            "stdout": stdout[:500],
            "stderr": stderr[:200],
            "error": error,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_process(watch: dict) -> dict:
    """
    check if a named process is running.

    config fields:
      name      -- process name to search for (matches against process name or cmdline)
      pid_file  -- path to a .pid file; check if the pid in it is alive (optional)
      match_full_cmdline -- if true, search the full command line string, not just the process name (default false)
    """
    process_name    = watch.get("name")
    pid_file        = watch.get("pid_file")
    match_full_cmd  = watch.get("match_full_cmdline", False)

    if not process_name and not pid_file:
        return {"ok": False, "error": "process watch requires 'name' or 'pid_file'"}

    # ── pid file path ──────────────────────────────────────────────────────────
    if pid_file:
        pid_path = Path(pid_file)
        if not pid_path.exists():
            return {
                "ok": False,
                "method": "pid_file",
                "pid_file": pid_file,
                "error": f"pid file not found: {pid_file}",
            }
        try:
            pid = int(pid_path.read_text().strip())
        except Exception as e:
            return {
                "ok": False,
                "method": "pid_file",
                "pid_file": pid_file,
                "error": f"could not read pid from file: {e}",
            }

        running, proc_info = _pid_is_running(pid)
        return {
            "ok": running,
            "method": "pid_file",
            "pid_file": pid_file,
            "pid": pid,
            "process_info": proc_info,
            "error": None if running else f"process with pid {pid} is not running",
        }

    # ── name-based search ──────────────────────────────────────────────────────
    matches = _find_processes_by_name(process_name, match_full_cmd)
    running = len(matches) > 0
    return {
        "ok": running,
        "method": "name",
        "process_name": process_name,
        "match_full_cmdline": match_full_cmd,
        "matching_pids": [p["pid"] for p in matches],
        "match_count": len(matches),
        "process_info": matches[0] if matches else None,
        "error": None if running else f"no running process found matching name '{process_name}'",
    }


def _pid_is_running(pid: int):
    """
    return (is_running: bool, info: dict).
    tries psutil first, falls back to /proc/<pid> or kill -0.
    """
    try:
        import psutil
        if psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                info = {
                    "pid": pid,
                    "name": p.name(),
                    "status": p.status(),
                    "cmdline": " ".join(p.cmdline())[:200],
                }
                return True, info
            except psutil.NoSuchProcess:
                return False, None
        return False, None
    except ImportError:
        pass

    # fallback: /proc/<pid> existence check (linux)
    proc_dir = Path(f"/proc/{pid}")
    if proc_dir.exists():
        try:
            cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace").strip()
            return True, {"pid": pid, "cmdline": cmdline[:200]}
        except Exception:
            return True, {"pid": pid}

    # last resort: kill -0 (works on unix, does not kill the process)
    try:
        os.kill(pid, 0)
        return True, {"pid": pid}
    except OSError:
        return False, None


def _find_processes_by_name(name: str, match_full_cmdline: bool = False) -> list:
    """
    return a list of dicts for each running process matching name.
    tries psutil, then falls back to /proc scanning, then pgrep.
    """
    name_lower = name.lower()

    # ── psutil ─────────────────────────────────────────────────────────────────
    try:
        import psutil
        matches = []
        for p in psutil.process_iter(["pid", "name", "cmdline", "status"]):
            try:
                pname   = (p.info.get("name") or "").lower()
                cmdline = " ".join(p.info.get("cmdline") or []).lower()
                if match_full_cmdline:
                    hit = name_lower in cmdline
                else:
                    hit = name_lower in pname
                if hit:
                    matches.append({
                        "pid": p.info["pid"],
                        "name": p.info.get("name"),
                        "status": p.info.get("status"),
                        "cmdline": " ".join(p.info.get("cmdline") or [])[:200],
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return matches
    except ImportError:
        pass

    # ── /proc scan (linux fallback) ────────────────────────────────────────────
    proc = Path("/proc")
    if proc.exists():
        matches = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                comm = (entry / "comm").read_text().strip().lower()
                cmdline_raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace").strip()
                if match_full_cmdline:
                    hit = name_lower in cmdline_raw.lower()
                else:
                    hit = name_lower in comm
                if hit:
                    matches.append({
                        "pid": pid,
                        "name": comm,
                        "cmdline": cmdline_raw[:200],
                    })
            except Exception:
                continue
        return matches

    # ── pgrep fallback ─────────────────────────────────────────────────────────
    try:
        flags = ["-a"] if match_full_cmdline else []
        out = subprocess.check_output(["pgrep", "-l"] + flags + [name], text=True, stderr=subprocess.DEVNULL)
        matches = []
        for line in out.strip().splitlines():
            parts = line.split(None, 1)
            if parts:
                pid_str = parts[0]
                pname   = parts[1] if len(parts) > 1 else name
                if pid_str.isdigit():
                    matches.append({"pid": int(pid_str), "name": pname, "cmdline": pname})
        return matches
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return []


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
}


def run_check(watch: dict) -> dict:
    wtype = watch.get("type")
    if wtype not in CHECKERS:
        return {"ok": False, "error": f"unknown watch type: {wtype}"}
    try:
        return CHECKERS[wtype](watch)
    except Exception as e:
        return {"ok": False, "error": f"checker raised exception: {e}"}


# ── llm evaluation ─────────────────────────────────────────────────────────────

def llm_evaluate(watch: dict, result: dict, history: list) -> dict:
    """
    call anthropic claude to decide if this result warrants an alert.
    returns {"alert": bool, "severity": str, "summary": str}
    on any failure, falls back to result["ok"] == False => alert.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # graceful fallback
        ok = result.get("ok", True)
        return {
            "alert": not ok,
            "severity": "warning" if not ok else "info",
            "summary": result.get("error") or ("all clear" if ok else "check failed"),
        }

    prompt = f"""you are a monitoring agent. evaluate this watch result and decide if it warrants an alert.

watch definition:
{json.dumps(watch, indent=2)}

current result:
{json.dumps(result, indent=2)}

recent history (last {len(history)} results):
{json.dumps(history, indent=2)}

respond with ONLY valid json in this exact format:
{{"alert": true or false, "severity": "info" or "warning" or "critical", "summary": "one line explanation"}}"""

    try:
        import urllib.request as req

        payload = json.dumps({
            "model": "claude-3-haiku-20240307",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        request = req.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with req.urlopen(request, timeout=20) as resp:
            body = json.loads(resp.read())

        text = body["content"][0]["text"].strip()
        # strip markdown code fences if present
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:])
            text = text.rstrip("`").strip()
        evaluation = json.loads(text)
        return evaluation

    except Exception as e:
        ok = result.get("ok", True)
        return {
            "alert": not ok,
            "severity": "warning" if not ok else "info",
            "summary": f"llm unavailable ({e}); raw result: {result.get('error') or 'ok'}",
        }


# ── runner ─────────────────────────────────────────────────────────────────────

def run_all(watches: list, dry_run: bool = False, verbose: bool = False):
    alerts = []

    for watch in watches:
        watch_id = watch.get("id", watch.get("name", "unknown"))
        wtype    = watch.get("type", "unknown")

        if verbose:
            print(f"  checking [{wtype}] {watch_id} ...", end=" ", flush=True)

        if dry_run:
            print(f"  [dry-run] would check [{wtype}] {watch_id}")
            continue

        result    = run_check(watch)
        history   = load_history(watch_id)
        evaluation = llm_evaluate(watch, result, history)

        ts = int(time.time())
        log_entry = {
            "ts":       ts,
            "watch_id": watch_id,
            "type":     wtype,
            "result":   result,
            "evaluation": evaluation,
            "alerted":  evaluation.get("alert", False),
        }
        append_log(log_entry)

        if verbose:
            status = "ALERT" if evaluation.get("alert") else "ok"
            print(f"{status} -- {evaluation.get('summary', '')}")

        if evaluation.get("alert"):
            alerts.append({
                "watch_id":  watch_id,
                "type":      wtype,
                "severity":  evaluation.get("severity", "warning"),
                "summary":   evaluation.get("summary", ""),
                "result":    result,
                "ts":        ts,
            })

    return alerts


def main():
    parser = argparse.ArgumentParser(description="watchdog runner")
    parser.add_argument("--dry-run",  action="store_true", help="collect but do not log or alert")
    parser.add_argument("--id",       help="run only the watch with this id")
    parser.add_argument("--verbose",  action="store_true", help="print each check as it runs")
    args = parser.parse_args()

    watches = load_watches()
    if not watches:
        sys.exit(0)

    if args.id:
        watches = [w for w in watches if w.get("id") == args.id or w.get("name") == args.id]
        if not watches:
            print(f"no watch found with id '{args.id}'")
            sys.exit(1)

    if args.verbose or args.dry_run:
        print(f"watchdog: running {len(watches)} watch(es)")

    alerts = run_all(watches, dry_run=args.dry_run, verbose=args.verbose)

    if alerts:
        print(f"\nwatchdog alerts ({len(alerts)}):")
        for a in alerts:
            ts_str = datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"  [{a['severity'].upper()}] {a['watch_id']} -- {a['summary']} ({ts_str})")
        sys.exit(2)
    else:
        if args.verbose:
            print("watchdog: all clear")
        sys.exit(0)


if __name__ == "__main__":
    main()