# watch type reference

full config schema for each watchdog watch type.

---

## http

polls an http(s) endpoint. checks status code, response time, and optionally body content.

```json
{
  "id":             "my-api",
  "name":           "production api",
  "type":           "http",
  "url":            "https://api.example.com/health",
  "expect_status":  200,
  "timeout_s":      10,
  "body_contains":  "ok",
  "enabled":        true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| url | yes | -- | full url including scheme |
| expect_status | no | 200 | expected http status code |
| timeout_s | no | 10 | request timeout in seconds |
| body_contains | no | -- | string that must appear in the response body |

---

## rss

polls an rss or atom feed. detects new entries and optionally keyword matches.

```json
{
  "id":       "hn-feed",
  "name":     "hacker news",
  "type":     "rss",
  "url":      "https://news.ycombinator.com/rss",
  "keyword":  "openclaw",
  "enabled":  true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| url | yes | -- | feed url |
| keyword | no | -- | keyword to match in entry titles. if set, alerts only on keyword hits |
| timeout_s | no | 10 | fetch timeout |

---

## system_disk

checks disk usage for a path. alerts when usage exceeds the threshold.

```json
{
  "id":                   "disk-home",
  "name":                 "home partition",
  "type":                 "system_disk",
  "path":                 "/home",
  "alert_threshold_pct":  85,
  "enabled":              true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| path | no | / | path to check usage for |
| alert_threshold_pct | no | 90 | alert when usage is above this percent |

---

## system_cpu

checks current cpu utilization. uses psutil if available, falls back to /proc/stat.

```json
{
  "id":                   "cpu-server",
  "name":                 "cpu load",
  "type":                 "system_cpu",
  "alert_threshold_pct":  85,
  "enabled":              true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| alert_threshold_pct | no | 90 | alert when cpu is above this percent |

---

## system_memory

checks memory utilization. uses psutil if available, falls back to /proc/meminfo.

```json
{
  "id":                   "mem-server",
  "name":                 "memory usage",
  "type":                 "system_memory",
  "alert_threshold_pct":  90,
  "enabled":              true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| alert_threshold_pct | no | 90 | alert when memory is above this percent |

---

## file

checks a file's existence, size, modification time, or content.

```json
{
  "id":           "lock-file",
  "name":         "process lock file",
  "type":         "file",
  "path":         "/var/run/myapp.pid",
  "expect_exists": true,
  "max_age_s":    3600,
  "contains":     "running",
  "enabled":      true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| path | yes | -- | absolute path to the file |
| expect_exists | no | true | alert if exists != this value |
| max_age_s | no | -- | alert if file has not been modified in this many seconds |
| contains | no | -- | string that must be present in the file contents |

---

## command

runs a shell command. evaluates exit code and optionally stdout content.

```json
{
  "id":              "service-check",
  "name":            "nginx status",
  "type":            "command",
  "command":         "systemctl is-active nginx",
  "expect_exit":     0,
  "output_contains": "active",
  "timeout_s":       10,
  "enabled":         true
}
```

| field | required | default | description |
|-------|----------|---------|-------------|
| command | yes | -- | shell command to run |
| expect_exit | no | 0 | expected exit code |
| output_contains | no | -- | string that must appear in stdout |
| timeout_s | no | 15 | kill the command after this many seconds |
