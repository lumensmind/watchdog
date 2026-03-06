# roadmap

the backlog. hourly commits work through this list.

## in progress

- [ ] v0.1.x -- core watch types working, llm eval, logging

## next up

### watch types
- [x] `process` watch -- check if a named process is running (by name or pid file)
- [x] `port` watch -- tcp connect check to host:port
- [x] `ssl_cert` watch -- days until ssl certificate expires
- [x] `json_api` watch -- poll a json api, evaluate a specific field with a condition (e.g. `status != "ok"`)
- [x] `ping` watch -- icmp ping with packet loss and latency

### features
- [x] `--since <time>` flag on runner -- only alert if condition has been true for at least N minutes (reduces noise)
- [x] `--summary` flag -- print a one-