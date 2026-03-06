# roadmap

the backlog. hourly commits work through this list.

## in progress

- [ ] v0.1.x — core watch types working, llm eval, logging

## next up

### watch types
- [x] `process` watch -- check if a named process is running (by name or pid file)
- [x] `port` watch -- tcp connect check to host:port
- [ ] `ssl_cert` watch -- days until ssl certificate expires
- [ ] `json_api` watch -- poll a json api, evaluate a specific field with a condition (e.g. `status != "ok"`)
- [ ] `ping` watch -- icmp ping with packet loss and latency

### features
- [ ] `--since <time>` flag on runner -- only alert if condition has been true for at least N minutes (reduces noise)
- [ ] `--summary` flag -- print a one-line status for every watch (good for daily digests)
- [ ] watch groups -- tag watches and run a group with `--group <tag>`
- [ ] alert cooldown -- don't re-alert the same watch within N minutes after an alert
- [ ] alert history -- `scripts/history.py` for viewing past alerts filtered by watch, date, severity
- [ ] digest mode -- `--digest` flag, returns a summary of all watch states for the last 24h

### integration
- [ ] openclaw heartbeat integration -- auto-run watchdog during heartbeat checks
- [ ] slack/discord notify -- push alert to a channel instead of just stdout
- [ ] webhook -- POST alert json to a configurable url
- [ ] openclaw cron setup helper -- `scripts/setup_cron.py` that registers the openclaw cron for you

### quality
- [ ] unit tests for each checker
- [ ] better error messages when watches.json is malformed
- [ ] validate watch config on add (required fields, valid types)
- [ ] `--version` flag
- [ ] proper packaging validation via skill-creator scripts

## done

- [x] initial http checker
- [x] rss/atom checker
- [x] system disk checker
- [x] system cpu checker (psutil + /proc/stat fallback)
- [x] system memory checker (psutil + /proc/meminfo fallback)
- [x] file checker (exists, age, content grep)
- [x] command checker (exit code + stdout match)
- [x] llm evaluation step (anthropic, graceful fallback)
- [x] jsonl log file
- [x] add_watch.py (interactive + --json mode)
- [x] build.py with github release creation
- [x] SKILL.md, README, references, example config
