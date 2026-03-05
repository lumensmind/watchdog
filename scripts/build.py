#!/usr/bin/env python3
"""
build.py -- package watchdog into a .skill file and create a github release.

usage:
    python3 scripts/build.py [--version 0.2.0] [--release] [--notes "what changed"]

requires: GITHUB_TOKEN env var for release creation.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

SKILL_DIR   = Path(__file__).parent.parent
DIST_DIR    = SKILL_DIR / "dist"
SKILL_NAME  = "watchdog"
REPO        = "lumensmind/watchdog"

# files and dirs to exclude from the .skill package
EXCLUDE = {
    ".git", ".gitignore", "dist", "watch_log.jsonl",
    "watches.json",   # user config -- not bundled
    "__pycache__", ".env",
}


def get_version(override: str = None) -> str:
    if override:
        return override
    # read from version file if it exists
    vfile = SKILL_DIR / "VERSION"
    if vfile.exists():
        return vfile.read_text().strip()
    return "0.1.0"


def bump_version(current: str) -> str:
    parts = current.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def build_skill(version: str) -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    out_path = DIST_DIR / f"{SKILL_NAME}.skill"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SKILL_DIR.rglob("*")):
            # skip excluded names at any level
            if any(ex in path.parts for ex in EXCLUDE):
                continue
            if path.is_file():
                arcname = path.relative_to(SKILL_DIR)
                zf.write(path, arcname)

    size_kb = round(out_path.stat().st_size / 1024, 1)
    print(f"built: {out_path} ({size_kb} KB)")
    return out_path


def create_github_release(version: str, skill_path: Path, notes: str = "") -> bool:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        print("no GITHUB_TOKEN found -- skipping release creation")
        return False

    import urllib.request
    import urllib.error

    tag = f"v{version}"
    today = date.today().isoformat()
    body = notes or f"nightly build {today}\n\ninstall: download watchdog.skill from assets"

    # create the release
    payload = json.dumps({
        "tag_name":         tag,
        "name":             f"v{version} -- {today}",
        "body":             body,
        "draft":            False,
        "prerelease":       False,
    }).encode()

    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases",
        data=payload,
        headers={
            "Authorization":  f"token {token}",
            "Content-Type":   "application/json",
            "Accept":         "application/vnd.github.v3+json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            release = json.loads(resp.read())
        upload_url = release["upload_url"].replace("{?name,label}", "")
        release_id = release["id"]
        print(f"created release: {release['html_url']}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"release creation failed: {e.code} -- {body}")
        return False

    # upload the .skill file as a release asset
    with open(skill_path, "rb") as f:
        asset_data = f.read()

    upload_req = urllib.request.Request(
        f"{upload_url}?name={skill_path.name}",
        data=asset_data,
        headers={
            "Authorization": f"token {token}",
            "Content-Type":  "application/zip",
            "Accept":        "application/vnd.github.v3+json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(upload_req) as resp:
            asset = json.loads(resp.read())
        print(f"uploaded asset: {asset['browser_download_url']}")
        return True
    except urllib.error.HTTPError as e:
        print(f"asset upload failed: {e.code} -- {e.read().decode()}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="version string (default: read from VERSION or bump)")
    parser.add_argument("--bump",    action="store_true", help="auto-increment patch version")
    parser.add_argument("--release", action="store_true", help="create github release after build")
    parser.add_argument("--notes",   default="", help="release notes")
    args = parser.parse_args()

    version = get_version(args.version)
    if args.bump and not args.version:
        version = bump_version(version)

    # write version file
    (SKILL_DIR / "VERSION").write_text(version + "\n")
    print(f"version: {version}")

    skill_path = build_skill(version)

    if args.release:
        create_github_release(version, skill_path, args.notes)

    print("done.")


if __name__ == "__main__":
    main()
