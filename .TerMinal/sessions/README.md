# .TerMinal/sessions/

Live session docs — the **central working state** for each Claude Code work
session. One directory per session: `NNNN-slug/session.md`, plus any scratch
files the session accumulates.

- **Start a session:** `/session-start "<goal>"` — allocates an id, seeds the
  doc with relevant tickets/research/prior-sessions/git-state, and generates a
  TDD-first checklist.
- **End a session:** `/session-end` — reconciles outcomes, cleans up, suggests
  follow-up tickets, captures documentation, and closes the doc.

Schema: [`.claude/skills/session-start/SESSION_EXAMPLE.md`](../../.claude/skills/session-start/SESSION_EXAMPLE.md)
(mirrored at `.codex/skills/session-start/SESSION_EXAMPLE.md`). Ids are
allocated atomically via `.claude/skills/session-start/bin/next-session-id` or
`.codex/skills/session-start/bin/next-session-id` (never hand-edit `.next-id`).
List with `.claude/skills/session-start/bin/sessions` or the Codex mirror.

Exactly one session should be `active` at a time — it is the single source of
truth for in-flight work.
