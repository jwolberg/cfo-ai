---
name: new-inbox-source
description: "Design or implement a durable TerMinal automation-inbox source (adapter, poller, webhook bridge, file watcher). Use on /new-inbox-source or when building a service that repeatedly enqueues requests."
---

# New Inbox Source

Use this when setting up a reusable source of automation requests: a Slack
poller, MR watcher, file-drop adapter, webhook bridge, or local script. For a
single one-off request, use `/automation-inbox` instead.

## Workflow

1. Identify the external source, trigger condition, repo scope, and desired
   action.
2. Choose the smallest durable adapter:
   - local script invoked by cron/launchd
   - file watcher
   - webhook bridge
   - poller with a persisted cursor
3. Enqueue requests with `terminal-cli inbox enqueue`.
4. Use stable `--source-id`, event `id`, and `dedupeKey` values.
5. Keep arbitrary shell out of inbox events; trigger an existing agent/script or
   file HITL for approval.
6. Document how to run, stop, and inspect the source.

## Command Shape

```bash
terminal-cli inbox enqueue \
  --source-id github:mr-watch \
  --source-name "GitHub MR watcher" \
  --source github \
  --type merge_request.opened \
  --title "Review MR #123" \
  --repo-root "$PWD" \
  --dedupe-key "github:repo:mr:123" \
  --action run-agent \
  --agent code-review \
  --engine codex
```

## Output Expectations

- Create or update the adapter code/config requested by the user.
- Keep credentials out of git.
- Add a short runbook or inline usage note if setup is non-obvious.
- Smoke-test with a cheap/no-op request when possible.
