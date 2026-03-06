#!/usr/bin/env python3
"""
add_watch.py -- interactively add a new watch to watches.json, or via --json flag.

usage:
    python3 scripts/add_watch.py
    python3 scripts/add_watch.py --json '{"type": "port", "host": "localhost", "port": 5432, ...}'
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

SKILL_DIR    = Path(__file__).parent.parent
WATCHES_FILE = SKILL_DIR / "watches.json"


def load_watches() -> dict:
    if WATCHES_FILE.exists():
        with open(WATCHES_FILE) as f:
            return json.load(f)
    return {"watches": []}


def save_watches(data: dict):
    with open(WATCHES_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"saved to {WATCHES_FILE}")


def make_id(label: str) -> str:
    slug = label.lower().replace(" ", "-")[:32]
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    return slug or str(uuid.uuid4())[:8]


def prompt_http() -> dict:
    url    = input("  url (e.g. https://example.com/health): ").strip()
    expect = input("  expected status code [200]: ").strip() or "200"
    match  = input("  body must contain (leave blank to skip): ").strip()
    w = {"type": "http", "url": url, "expect_status": int(expect)}
    if match:
        w["body_contains"] = match
    return w


def prompt_rss() -> dict:
    url     = input("  feed url: ").strip()
    keyword = input("  keyword to match (leave blank to skip): ").strip()
    w = {"type": "rss", "url": url}
    if keyword:
        w["keyword"] = keyword
    return w


def prompt_system_disk() -> dict:
    path = input("  path to check [/]: ").strip() or "/"
    warn = input("  warn above % [80]: ").strip() or "80"
    return {"type": "system_disk", "path": path, "warn_above_pct": int(warn)}


def prompt_system_cpu() -> dict:
    warn = input("  warn above % [90]: ").strip() or "90"
    return {"type": "system_cpu", "warn_above_pct": int(warn)}


def prompt_system_memory() -> dict:
    warn = input("  warn above % [90]: ").strip() or "90"
    return {"type": "system_memory", "warn_above_pct": int(warn)}


def prompt_file() -> dict:
    path      = input("  file path: ").strip()
    max_age   = input("  max age in seconds (leave blank to skip): ").strip()
    contains  = input("  file must contain text (leave blank to skip): ").strip()
    w = {"type": "file", "path": path}
    if max_age:
        w["max_age_s"] = int(max_age)
    if contains:
        w["contains"] = contains
    return w


def prompt_command() -> dict:
    cmd    = input("  shell command: ").strip()
    expect = input("  expected exit code [0]: ").strip() or "0"
    match  = input("  output must contain (leave blank to skip): ").strip()
    w = {"type": "command", "command": cmd, "expect_exit": int(expect)}
    if match:
        w["output_contains"] = match
    return w


def prompt_process() -> dict:
    name     = input("  process name (leave blank if using pid file): ").strip()
    pid_file = input("  pid file path (leave blank to skip): ").strip()
    require  = input("  require running? [yes]: ").strip().lower()
    require  = require not in ("no", "n", "false")
    w = {"type": "process", "require_running": require}
    if name:
        w["name"] = name
    if pid_file:
        w["pid_file"] = pid_file
    return w


def prompt_port() -> dict:
    host    = input("  host [localhost]: ").strip() or "localhost"
    port    = input("  port number: ").strip()
    timeout = input("  timeout in seconds [10]: ").strip() or "10"
    w = {
        "type": "port",
        "host": host,
        "port": int(port),
        "timeout_s": int(timeout),
    }
    return w


PROMPTS = {
    "http":          prompt_http,
    "rss":           prompt_rss,
    "system_disk":   prompt_system_disk,
    "system_cpu":    prompt_system_cpu,
    "system_memory": prompt_system_memory,
    "file":          prompt_file,
    "command":       prompt_command,
    "process":       prompt_process,
    "port":          prompt_port,
}


def interactive_add():
    print("add a new watch")
    print("---------------")
    types = list(PROMPTS.keys())
    for i, t in enumerate(types, 1):
        print(f"  {i}. {t}")
    choice = input("watch type: ").strip().lower()

    # allow numeric pick
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(types):
            choice = types[idx]

    if choice not in PROMPTS:
        print(f"unknown type: {choice}")
        sys.exit(1)

    watch = PROMPTS[choice]()
    label = input("label (short description): ").strip()
    watch["id"]      = make_id(label or choice)
    watch["label"]   = label or choice
    watch["enabled"] = True

    data = load_watches()
    data["watches"].append(watch)
    save_watches(data)
    print(f"added watch: {watch['id']}")


def json_add(raw: str):
    try:
        watch = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"invalid json: {e}")
        sys.exit(1)

    if "id" not in watch:
        watch["id"] = make_id(watch.get("label", watch.get("type", "watch")))
    if "enabled" not in watch:
        watch["enabled"] = True

    data = load_watches()
    data["watches"].append(watch)
    save_watches(data)
    print(