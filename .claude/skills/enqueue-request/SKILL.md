---
name: enqueue-request
description: "Enqueue a one-off TerMinal automation-inbox request (can create Activity, tickets, HITL, agent runs, or background tasks). Use on /enqueue-request or to queue a durable local automation request."
---

# Enqueue Request

Enqueue one-off durable requests into TerMinal's Automation Inbox. It is the
always-on intake path for external events: use agents for manual runs, schedules
for time-based runs, and the Automation Inbox for queued requests from scripts
or integrations.

The app watches by default:

`~/.config/TerMinal/automation-inbox/new/`

Files move through `processing/`, then `done/`, `failed/`, or `dead-letter/`.

## Preferred Command

```bash
terminal-cli inbox enqueue \
  --source-id local-script:repo-health \
  --source-name "Local repo health" \
  --source local-script \
  --type automation.requested \
  --title "Run repo health" \
  --repo-root "$PWD" \
  --action run-agent \
  --agent health \
  --engine codex
```

Useful helpers:

```bash
terminal-cli inbox example
terminal-cli inbox status
terminal-cli inbox dir
```

## Envelope

Required:
- `source`: producer id, such as `local-script`, `slack`, `github`.
- `type`: event type, such as `automation.requested` or `merge_request.opened`.

Recommended:
- `listenerId`: stable source id. The CLI flag is `--source-id`.
- `listenerName`: human source label. The CLI flag is `--source-name`.
- `id`: stable event id. If omitted, the CLI assigns one.
- `dedupeKey`: stable idempotency key for external events.
- `repoRoot`: absolute repo path for actions that touch a repo.
- `title`, `body`: human-readable request text.
- `requestedAction`: one allowlisted action.

## Actions

```json
{ "kind": "activity", "activityKind": "info", "title": "Observed event", "detail": "..." }
{ "kind": "file-ticket", "title": "Fix webhook", "body": "...", "type": "bug", "priority": "medium" }
{ "kind": "file-hitl", "title": "Approval needed", "action": "Approve deploy" }
{ "kind": "run-agent", "agentId": "health", "engine": "codex", "mode": "agent" }
{ "kind": "run-agent", "agentId": "health", "engine": "codex", "mode": "background", "prompt": "..." }
{ "kind": "background-task", "engine": "claude", "prompt": "..." }
```

Do not put arbitrary shell in inbox requests. If shell is needed, create an
agent/script first and trigger it by id.

## Safety

- Use `dedupeKey` for polling/webhook integrations.
- Let invalid requests land in `failed/` or `dead-letter/`; do not retry by
  rewriting the same bad file in a loop.
- For destructive/costly actions, file HITL instead of running directly.
