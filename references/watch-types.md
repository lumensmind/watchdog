# watch types reference

full config schema for each supported watch type.

---

## http

poll an http endpoint and check status, response time, or body content.

```json
{
  "id": "my-api-health",
  "type": "http",
  "name": "my api health",
  "url": "https://api.example.com/health",
  "expect_status": 200,
  "body_contains": "ok",
  "timeout_s": 10,
  "enabled": true
}
```

fields:
- `url` (required) -- full url to GET
- `expect_status` (default 200) -- expected http status code
- `body_contains` -- string that must appear in the response body
- `timeout_s` (default 10) -- request timeout in seconds

---

## rss

poll an rss or atom feed. detect new entries or keyword matches.

```json
{
  "id": "hn-top",
  "type": "rss",
  "name": "hacker news top",
  "url": "https://news.ycombinator.com/rss",
  "keyword": "openai",
  "enabled": true
}
```

fields:
- `url` (required) -- feed url
- `keyword` -- if set, flag entries whose title or description contains this string (case-insensitive)

---

## system_disk

check disk usage for a mount point.

```json
{
  "id": "root-disk",
  "type": "system_disk",
  "name": "root disk usage",
  "path": "/",
  "warn_percent": 80,
  "critical_percent": 90,
  "enabled": true
}
```

fields:
- `path` (default `/`) -- mount point to check
- `warn_percent` (default 80) -- warning threshold
- `critical_percent` (default 90) -- critical threshold; result is `ok: false` above this

---

## system_cpu

check current cpu utilization.

```json
{
  "id": "cpu-check",
  "type": "system_cpu",
  "name": "cpu usage",
  "warn_percent": 80,
  "critical_percent": 95,
  "enabled": true
}
```

fields:
- `warn_percent` (default 80) -- warning threshold
- `critical_percent` (default 95) -- critical threshold; result is `ok: false` above this

---

## system_memory

check current memory utilization.

```json
{
  "id": "mem-check",
  "type": "system_memory",
  "name": "memory usage",
  "warn_percent": 80,
  "critical_percent": 95,
  "enabled": true
}
```

fields:
- `warn_percent` (default 80) -- warning threshold
- `critical_percent` (default 95) -- critical threshold; result is `ok: false` above this

---

## file

check a file for existence, age, size, or content.

```json
{
  "id": "app-log-fresh",
  "type": "file",
  "name": "app log freshness",
  "path": "/var/log/myapp/app.log",
  "max_age_seconds": 3600,
  "grep": "ERROR",
  "enabled": true
}
```

fields:
- `path` (required) -- absolute or relative path to the file
- `max_age_seconds` -- alert if file has not been modified within this many seconds
- `grep` -- alert if this string is NOT found in the file contents

---

## command

run a shell command and check its exit code or output.

```json
{
  "id": "backup-check",
  "type": "command",
  "name": "last backup check",
  "command": "ls -t /backups/*.tar.gz | head -1",
  "expect_exit_code": 0,
  "stdout_contains": ".tar.gz",
  "timeout_s": 30,
  "enabled": true
}
```

fields:
- `command` (required) -- shell command to run
- `expect_exit_code` (default 0) -- expected exit code
- `stdout_contains` -- string that must appear in stdout
- `timeout_s` (default 30) -- command timeout in seconds

---

## process

check if a named process is running, by process name or pid file.

```json
{
  "id": "nginx-running",
  "type": "process",
  "name": "nginx",
  "enabled": true
}
```

or with a pid file:

```json
{
  "id": "myapp-running",
  "type": "process",
  "name": "myapp process",
  "pid_file": "/var/run/myapp.pid",
  "enabled": true
}
```

or matching against the full command line (useful for python scripts or java apps):

```json
{
  "id": "worker-running",
  "type": "process",
  "name": "worker",
  "match_full_cmdline": true,
  "enabled": true
}
```

fields:
- `name` -- process name to search for. matched against the process name field (or full cmdline if `match_full_cmdline` is true). required if `pid_file` is not set.
- `pid_file` -- path to a pid file. if provided, the pid inside is read and checked for liveness. takes priority over name-based search.
- `match_full_cmdline` (default false) -- if true, match `name` against the full command