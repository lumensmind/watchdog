#!/usr/bin/env python3
"""
add_watch.py -- add a new watch to watches.json.

usage:
    python3 scripts/add_watch.py                     # interactive
    python3 scripts/add_watch.py --json '{"id": "my-api", "type": "http", ...}'
    python3 scripts/add_watch.py --list              # show existing watches
    python3 scripts/add_watch.py --remove <id>       # remove a watch
    python3 scripts/add_watch.py --toggle <id>       # enable/disable a watch
"""

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR    = Path(__file__).parent.parent
WATCHES_FILE = SKILL_DIR / "watches.json"


def load_config() -> dict:
    if WATCHES_FILE.exists():
        with open(WATCHES_FILE) as f:
            return json.load(f)
    return {"watches": []}


def save_config(config: dict):
    with open(WATCHES_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"saved to {WATCHES_FILE}")


def list_watches(config: dict):
    watches = config.get("watches", [])
    if not watches:
        print("no watches configured.")
        return
    print(f"\n{len(watches)} watch(es):\n")
    for w in watches:
        status = "on" if w.get("enabled", True) else "off"
        print(f"  [{status}] {w['id']:25s} {w['type']:15s} {w.get('name', '')}")
    print()


def interactive_add() -> dict:
    """Walk the user through adding a watch interactively."""
    print("\nadd a watch -- press ctrl+c to cancel\n")

    watch_types = ["http", "rss", "system_disk", "system_cpu", "system_memory", "file", "command"]
    print("watch types: " + ", ".join(watch_types))
    wtype = input("type: ").strip().lower()
    if wtype not in watch_types:
        print(f"unknown type '{wtype}'")
        sys.exit(1)

    watch_id = input("id (slug, no spaces): ").strip().lower().replace(" ", "-")
    name     = input("name (friendly label): ").strip() or watch_id
    watch = {"id": watch_id, "name": name, "type": wtype, "enabled": True}

    if wtype == "http":
        watch["url"]            = input("url: ").strip()
        watch["expect_status"]  = int(input("expected status code [200]: ").strip() or "200")
        watch["timeout_s"]      = int(input("timeout seconds [10]: ").strip() or "10")
        body_check = input("body must contain (leave blank to skip): ").strip()
        if body_check:
            watch["body_contains"] = body_check

    elif wtype == "rss":
        watch["url"]     = input("feed url: ").strip()
        keyword = input("keyword to watch for (leave blank for any new entry): ").strip()
        if keyword:
            watch["keyword"] = keyword

    elif wtype == "system_disk":
        watch["path"]               = input("path [/]: ").strip() or "/"
        watch["alert_threshold_pct"] = int(input("alert when above % [85]: ").strip() or "85")

    elif wtype == "system_cpu":
        watch["alert_threshold_pct"] = int(input("alert when above % [90]: ").strip() or "90")

    elif wtype == "system_memory":
        watch["alert_threshold_pct"] = int(input("alert when above % [90]: ").strip() or "90")

    elif wtype == "file":
        watch["path"]          = input("file path: ").strip()
        watch["expect_exists"] = True
        max_age = input("alert if not modified in N seconds (leave blank to skip): ").strip()
        if max_age:
            watch["max_age_s"] = int(max_age)
        grep = input("alert if file does not contain (leave blank to skip): ").strip()
        if grep:
            watch["contains"] = grep

    elif wtype == "command":
        watch["command"]      = input("shell command: ").strip()
        watch["expect_exit"]  = int(input("expected exit code [0]: ").strip() or "0")
        output_check = input("output must contain (leave blank to skip): ").strip()
        if output_check:
            watch["output_contains"] = output_check
        watch["timeout_s"] = int(input("timeout seconds [15]: ").strip() or "15")

    return watch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json",   help="add watch from json string")
    parser.add_argument("--list",   action="store_true", help="list all watches")
    parser.add_argument("--remove", metavar="ID", help="remove a watch by id")
    parser.add_argument("--toggle", metavar="ID", help="toggle a watch on/off")
    args = parser.parse_args()

    config = load_config()

    if args.list:
        list_watches(config)
        return

    if args.remove:
        before = len(config["watches"])
        config["watches"] = [w for w in config["watches"] if w["id"] != args.remove]
        if len(config["watches"]) == before:
            print(f"no watch with id '{args.remove}'")
            sys.exit(1)
        save_config(config)
        print(f"removed: {args.remove}")
        return

    if args.toggle:
        for w in config["watches"]:
            if w["id"] == args.toggle:
                w["enabled"] = not w.get("enabled", True)
                save_config(config)
                print(f"{'enabled' if w['enabled'] else 'disabled'}: {args.toggle}")
                return
        print(f"no watch with id '{args.toggle}'")
        sys.exit(1)

    if args.json:
        new_watch = json.loads(args.json)
    else:
        new_watch = interactive_add()

    # check for duplicate id
    existing_ids = {w["id"] for w in config["watches"]}
    if new_watch["id"] in existing_ids:
        print(f"watch id '{new_watch['id']}' already exists. use --remove to delete it first.")
        sys.exit(1)

    config["watches"].append(new_watch)
    save_config(config)
    print(f"\nadded: {new_watch['id']} ({new_watch['type']})")
    print(f"test it: python3 scripts/watch_runner.py --id {new_watch['id']} --verbose")


if __name__ == "__main__":
    main()
