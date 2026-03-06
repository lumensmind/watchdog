# watch types reference

full config schema for each supported watch type.

---

## http

poll an http endpoint. checks status code, response time, and optional body content.

```json
{
  "id": "my-api-health",
  "name": "my api health",
  "type": "http",
  "enabled": true,
  "url": "https://api.example.com/health",
  "timeout_s": 10,
  "expect_status": 200,
  "body_contains": "ok"
}
```

fields:
- `url` (required) -- full url to GET
- `timeout_s` (default: 10) -- request timeout in seconds
- `expect_status` (default: 200) -- expected http status code
- `body_contains` (optional) -- string that must appear in the first 4kb of the response body

---

## rss

poll an rss or atom feed. detects new entries and optionally matches keywords.

```json
{
  "id": "hn-feed",
  "name": "hacker news",
  "type": "rss",
  "enabled": true,
  "url": "https://news.ycombinator.com/rss",
  "keyword": "llm"
}
```

fields:
- `url` (required) -- feed url
- `keyword` (optional) -- if set, flag entries where title or description contains this string (case-insensitive)

---

## system_disk

check disk usage for a path.

```json
{
  "id": "disk-root",
  "name": "root disk usage",
  "type": "system_disk",
  "enabled": true,
  "path": "/",
  "warn_above_pct": 85
}
```

fields:
- `path` (default: `/`) -- filesystem path to check
- `warn_above_pct` (default: 80) -- alert if usage exceeds this percentage

---

## system_cpu

check current cpu usage.

```json
{
  "id": "cpu-load",
  "name": "cpu load",
  "type": "system_cpu",
  "enabled": true,
  "warn_above_pct": 90
}
```

fields:
- `warn_above_pct` (default: 90) -- alert if cpu percent exceeds this value

---

## system_memory

check current memory usage.

```json
{
  "id": "mem-usage",
  "name": "memory usage",
  "type": "system_memory",
  "enabled": true,
  "warn_above_pct": 90
}
```

fields:
- `warn_above_pct` (default: 90) -- alert if memory percent exceeds this value

---

## file

check a file exists, its age, and optionally that it contains a string.

```json
{
  "id": "app-lock",
  "name": "app lock file",
  "type": "file",
  "enabled": true,
  "path": "/var/run/myapp.lock",
  "max_age_s": 3600,
  "contains": "running"
}
```

fields:
- `path` (required) -- absolute path to the file
- `max_age_s` (optional) -- alert if file is older than this many seconds
- `contains` (optional) -- alert if this string is not found in the file

---

## command

run a shell command and check its exit code and/or stdout.

```json
{
  "id": "backup-check",
  "name": "backup script check",
  "type": "command",
  "enabled": true,
  "command": "bash /home/user/check_backup.sh",
  "expect_exit_code": 0,
  "output_contains": "success",
  "timeout_s": 30
}
```

fields:
- `command` (required) -- shell command to run
- `expect_exit_code` (default: 0) -- expected exit code
- `output_contains` (optional) -- string that must appear in stdout
- `timeout_s` (default: 30) -- max seconds to wait for the command

---

## process

check if a named process is running, by process name or pid file.

```json
{
  "id": "nginx-proc",
  "name": "nginx process",
  "type": "process",
  "enabled": true,
  "name": "nginx",
  "pid_file": "/var/run/nginx.pid"
}
```

fields:
- `name` (optional) -- process name or substring to match in process list (uses psutil or pgrep)
- `pid_file` (optional) -- path to a pid file; checks that the pid in the file is running
- at least one of `name` or `pid_file` must be set

---

## port

tcp connect check to a host and port.

```json
{
  "id": "postgres-port",
  "name": "postgres tcp",
  "type": "port",
  "enabled": true,
  "host": "localhost",
  "port": 5432,
  "timeout_s": 5
}
```

fields:
- `host` (default: `localhost`) -- hostname or ip to connect to
- `port` (required) -- tcp port number
- `timeout_s` (default: 10) -- connection timeout in seconds

---

## ssl_cert

check an ssl certificate's expiry date. alerts when the certificate is expiring soon.

```json
{
  "id": "my-site-ssl",
  "name": "my site ssl cert",
  "type": "ssl_cert",
  "enabled": true,
  "host": "example.com",
  "port": 443,
  "warn_below_days": 14,
  "timeout_s": 10
}
```

fields:
- `host` (required) -- hostname to connect to and check the certificate for
- `port` (default: 443) -- port to connect on (almost always 443)
- `warn_below_days` (default: 14) -- alert if the certificate expires within this many days
- `timeout_s` (default: 10) -- connection timeout in seconds

result fields:
- `ok` -- false if cert expires within warn_below_days or connection failed
- `days_remaining` -- integer days until expiry
- `expires` -- raw expiry string from the certificate
- `error` -- set if connection failed or cert could not be parsed