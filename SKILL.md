---
name: watchdog
description: Proactive monitoring skill for OpenClaw agents. Use when the user wants to monitor HTTP endpoints, RSS feeds, system metrics (CPU/disk/memory), files, or custom commands and receive intelligent alerts when something is wrong or noteworthy. Triggers on: "watch this", "monitor my server", "alert me if", "check my API", "tell me when my disk is full", "watchdog", "add a watch", "run my watches", or any request to set up proactive monitoring or alerting.
---

# Watchdog

Proactive monitoring for OpenClaw agents. Watches data sources on a schedule, evaluates results with the LLM, and surfaces only what's actually worth knowing.

## Quick start

```bash
# run all active watches and evaluate results
python3 scripts/watch_runner.py

# dry run -- shows what would be checked and collected, no alerts sent
python3 scripts/watch_runner.py --dry-run

# add a new watch interactively
python3 scripts/add_watch.py

# check one specific watch by id
python3 scripts/watch_runner.py --id <watch-id>
```

## Watch config location

Watches are stored in `watches.json` in the skill directory. Create it from `assets/watches.example.json` if it doesn't exist.

## Watch types

See `references/watch-types.md` for full config schema per type. Supported:

- `http` -- GET request, check status code, response time, body content
- `rss` -- poll feed, detect new entries, keyword match in titles/descriptions
- `system_cpu` -- current cpu percent, rolling average
- `system_disk` -- disk usage percent for a path
- `system_memory` -- memory usage percent
- `file` -- file exists, size, mtime, content hash or grep match
- `command` -- run shell command, evaluate exit code and/or stdout

## Adding watches

Ask the agent to add a watch naturally:

> "watch my API at https://api.example.com/health"
> "alert me if disk on /home goes above 90%"
> "monitor my rss feed at https://news.ycombinator.com/rss"

The agent should call `add_watch.py` with `--json '{...}'` for programmatic adds, or run it interactively.

## Evaluating results

The runner collects raw results from all watches, then calls the LLM with:
- the watch definition (what you care about)
- the current result (what was found)
- recent history (last N results for context)

The LLM returns: `alert: true/false`, `severity: info/warning/critical`, `summary: <one line>`.

Only `alert: true` results are surfaced to the user. Everything is logged to `watch_log.jsonl`.

## Scheduling

To run watchdog automatically, register it with openclaw cron:

```
openclaw cron add "run watchdog" --every 15m
```

Or for hourly:
```
openclaw cron add "run watchdog" --every 1h
```

The agent will run `watch_runner.py`, evaluate results, and message you only if something needs attention.

## Log file

All results (alerted or not) are appended to `watch_log.jsonl` for history and LLM context. Format:

```json
{"ts": 1700000000, "watch_id": "api-health", "type": "http", "result": {...}, "alerted": false}
```
