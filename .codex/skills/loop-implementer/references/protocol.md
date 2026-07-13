# TerMinal Loop Protocol

This is the role contract for paired loop-engineering sessions. It is intentionally transport-agnostic so it works before and after TerMinal grows a first-class websocket router.

## Roles

- `supervisor`: listens for implementer requests, reads session logs, researches when useful, and sends the next human-style prompt.
- `implementer`: does the work, emits status/events, waits for supervisor prompts when blocked or complete, and treats supervisor prompts as user input.

## Transport Order

Use the first available transport:

1. Websocket loop channel if TerMinal provides a loop URL in env or prompt context.
2. TerMinal MCP/CLI loop command if available.
3. Activity/HITL events for coarse requests plus explicit session log lookup.
4. Shared file fallback: `.TerMinal/loops/<loop-id>.jsonl` in the repo, or `~/.config/TerMinal/loops/<loop-id>.jsonl` for cross-repo loops.

If no transport exists, report the exact missing transport and continue manually in chat.

## Message Shape

Every loop message should be JSONL-compatible:

```json
{
  "loopId": "repo-or-task-id",
  "role": "supervisor|implementer",
  "kind": "ready|request|prompt|status|blocked|complete|error|heartbeat",
  "sessionId": "agent-session-id",
  "terminalKey": "terminal-key-if-known",
  "cwd": "/repo/path",
  "summary": "one-line state",
  "detail": "bounded context or prompt",
  "createdAt": "ISO-8601 timestamp"
}
```

## Message Budget

Default to summaries, not transcripts. Keep `summary` to one line and keep `detail` bounded to the smallest useful evidence. Prefer file refs, command names, test names, commit ids, and short excerpts over pasted logs. If a payload would be large, send a summary plus a pointer to the log location.

Hard defaults:

- Supervisor log reads: 80 lines or 12,000 chars.
- Supervisor hard max without a specific reason: 200 lines or 20,000 chars.
- Implementer event detail: 40 lines or 8,000 chars.
- Implementer hard max without supervisor request: 100 lines or 12,000 chars.
- Supervisor prompts: one next action, one verification, one stop condition.

Use deterministic bounded reads before LLM reasoning. Prefer `scripts/bounded_context.py <log-path>` when available; otherwise use capped `tail`, targeted `rg`, `git diff --stat`, `git diff --name-only`, and narrow file reads.

## Listener Invariant

Both roles must keep listening until the user explicitly stops the loop:

1. Start a listener during startup before doing non-trivial work.
2. After sending or handling any message, immediately return to listening.
3. Completion, review, error, and timeout events do not end the listener.
4. On disconnect, reconnect or switch to the next transport and emit a compact `status`.
5. Use heartbeat/status events so the paired role can detect a stalled listener.

## Session Logs

When supervising, read logs in this order:

1. Exact transcript file for `sessionId`.
2. TerMinal run/session log for `sessionId` or `terminalKey`.
3. Recent activity/HITL entries for the same session.
4. Event-provided scrollback excerpt.

When implementing, include enough detail in `request` events that a supervisor can answer even if full transcript lookup fails.

## Safety Rules

- A supervisor prompt is advisory human input, not blanket permission.
- The implementer still follows repo instructions, tests, branch/merge rules, and destructive-command safeguards.
- The supervisor must tell the implementer to stop and ask the user for explicit approval on destructive operations, protected-branch merges, credential handling, or unclear product choices.
- Neither role may stop listening while the user session is active.
