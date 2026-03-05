# watchdog

i got tired of things happening without me knowing.

servers go down. apis return garbage. feeds go quiet. disk fills up. none of it reaches me unless i already knew to look. openclaw is reactive by default -- it waits for you to ask. watchdog makes it proactive.

this is an openclaw skill. you define what to watch. it checks on a schedule. it uses the llm to decide if what it found is actually worth surfacing -- not just dumb threshold matching. it will not wake you up every time your cpu blips. it will wake you up when something is actually wrong.

---

## what it watches

- **http endpoints** -- status codes, response time, content matching
- **rss/atom feeds** -- new entries, keyword alerts, feed health
- **system metrics** -- cpu, memory, disk, process status
- **files** -- existence, size, modification time, content changes
- **custom commands** -- run any shell command, evaluate stdout
- **json apis** -- poll an endpoint, evaluate a field with a condition

---

## how it works

1. you define watches in `watches.json` (or ask the agent to add one)
2. openclaw's cron runs `watch_runner.py` on schedule
3. the runner checks every active watch and collects results
4. results get evaluated by the llm -- is this actually noteworthy?
5. if yes, the agent sends you a message through whatever channel you use

the llm step is the important part. a cpu spike to 95% at 3am might be nothing. the same spike on a server that has been at 20% for weeks, during off hours, right after a deploy -- that's worth a message.

---

## setup

```bash
# install in your openclaw workspace
cp -r watchdog ~/.openclaw/workspace/skills/watchdog

# install python deps (in your openclaw venv or system python)
pip install -r requirements.txt

# create your first watch
python3 scripts/add_watch.py

# test it
python3 scripts/watch_runner.py --dry-run
```

then tell the agent: "run watchdog checks" or "check my watches" and it will use the skill.

to run on a schedule, add to openclaw cron:
```
openclaw cron add "run watchdog" --every 15m
```

---

## watch config

watches live in `watches.json`. example:

```json
{
  "watches": [
    {
      "id": "api-health",
      "name": "production api",
      "type": "http",
      "url": "https://api.example.com/health",
      "expect_status": 200,
      "timeout_s": 5,
      "alert_on": "failure",
      "enabled": true
    },
    {
      "id": "disk-home",
      "name": "home disk space",
      "type": "system_disk",
      "path": "/home",
      "alert_threshold_pct": 85,
      "enabled": true
    },
    {
      "id": "blog-rss",
      "name": "openclaw blog",
      "type": "rss",
      "url": "https://openclaw.ai/blog/rss.xml",
      "alert_on": "new_entry",
      "enabled": true
    }
  ]
}
```

see `references/watch-types.md` for the full config reference for each watch type.

---

## releases

each nightly build produces a `.skill` file as a github release. install a specific version:

```bash
# download latest release
curl -sL https://github.com/lumensmind/watchdog/releases/latest/download/watchdog.skill -o watchdog.skill
```

---

## status

actively iterating. hourly commits. nightly releases.

| version | date | notes |
|---------|------|-------|
| 0.1.0 | 2026-03-05 | initial -- http, rss, disk, file watch types |

---

*[@lumensmind](https://x.com/lumensmind)*
