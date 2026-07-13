---
name: session-start
description: "Open a work session: allocate an id, seed a live session doc by scanning the repo for relevant tickets/state, and generate a TDD-first checklist. Use on /session-start \"<goal>\" or to kick off work."
---

# /session-start — Open a session and seed its live doc

## Fast path: TerMinal MCP tools

When the `terminal-harness` MCP server is registered, use these for the
deterministic mechanics in this skill:

- **`update_ticket({slug, status: 'in-progress'})`** for each in-scope ticket
  as soon as the session starts.
- **`emit_activity({kind: 'session-start', title: '<goal>', repo})`** so the
  Activity tab + Telegram feed get the start signal.

The thinker work (scanning the repo for relevant tickets/research/tools,
drafting the [2] Context section, generating the TDD checklist) stays
model-side — that's the whole point of this skill.

---

Creates the **central live-state document** for a work session and front-loads
all the context needed to work well. Invoked as:

```
/session-start "Tackle the vault tickets 0012–0014"
```

The argument is the session **goal** (verbatim → `goal:` in frontmatter). The
session doc lives at `.TerMinal/sessions/<id>-<slug>/session.md` in v2 repos
(legacy v1: `sessions/<id>-<slug>/session.md`) and follows the strict schema in
[`SESSION_EXAMPLE.md`](./SESSION_EXAMPLE.md). It is the single source of truth
for the session — every later skill (and `/session-end`) reads and updates it.

## Process

### 1. Allocate the session id

```bash
ROOT="$(git rev-parse --show-toplevel)"
SESSIONS_DIR=$([ -d "$ROOT/sessions" ] && [ ! -f "$ROOT/.TerMinal/template.json" ] && echo "$ROOT/sessions" || echo "$ROOT/.TerMinal/sessions")
id=$("$ROOT/.claude/skills/session-start/bin/next-session-id")   # e.g. 0007
```

Never hand-edit `.next-id`. Derive a kebab `slug` (≤ 6 words) from the goal.
Create the directory: `mkdir -p "$SESSIONS_DIR/${id}-${slug}"`.

### 2. Seed context (the high-value step)

This is what makes a session start well. Scan the repo **read-only** and gather
only goal-relevant context. Run these in parallel where possible and keep the
model context capped:

- **In-scope tickets** — `"$ROOT/.claude/skills/ticket/bin/tickets" open` and
  `... in-progress`. Pull the ones matching the goal (by id if the user named
  them, else by keyword). Read at most 8 ticket bodies; for each, capture the
  one-line title + acceptance criteria. These drive the checklist.
- **Research & docs** — use path/snippet search first, not full-file reads:
  `rg -n -i -C2 "<goal keywords>" docs research 2>/dev/null | head -160`.
  List relevant ADRs (`docs/decisions/`), runbooks (`docs/runbooks/`),
  learnings (`docs/learnings/`), and `docs/architecture.md` sections. Read
  only the files that match.
- **Existing tools / skills** — note which `.claude/skills/` and repo `bin/`
  tools apply to this goal so we reuse instead of rebuild. List names first
  (`find .claude/skills -maxdepth 2 -name SKILL.md | head -40`) and open only
  directly relevant skill docs.
- **Prior sessions** — `"$ROOT/.claude/skills/session-start/bin/sessions"`.
  Read at most 3 recent or keyword-related session docs, especially their
  **Follow-ups** sections — unfinished work and suggested tickets carry
  forward. Link them in `prior_sessions`.
- **Git / PR state** — current branch, `git log --oneline -10`, and open PRs
  (`gh pr list`). Note anything in flight that this session interacts with. If
  any merged PRs are still linked to non-closed tickets, suggest running
  `/merge-sync` to reconcile before starting.

### 3. Generate the checklist (TDD-first)

From the in-scope tickets' acceptance criteria, write a concrete, ordered TODO
list. **TDD is mandatory and explicit**: every behavior change gets a "write the
failing test" item *before* its implementation item, then a "make it pass" item.
End each ticket's run with "open PR + link to ticket". Example:

```
- [ ] 0012 — write failing test: vault create rejects spend over cap
- [ ] 0012 — implement vault-create to make the test pass
- [ ] 0012 — open PR + link PR url into ticket 0012 prs:
```

Keep it realistic for one session; defer the rest to Follow-ups.

### 4. Write the session doc

Write `$SESSIONS_DIR/<id>-<slug>/session.md` using the SESSION_EXAMPLE schema.
Populate **all** frontmatter fields — `id`, `slug`, `anchor: SES-<zero-padded
id>`, `title` (a short headline from the goal — the listers display it), `status:
active`, `started:` now, `ended: null`, `goal` (verbatim), `tickets`,
`branches: []`, `prs: []`, `related_research`, `related_docs`, `prior_sessions`
— then the **[1] Goal**,
**[2] Context & pointers**, and **[3] Checklist** sections. Leave [4]–[8] as
stubs for the session and `/session-end` to fill. **Anchor every heading**
(`[N]` / `[N.M]`) per the anchoring convention so the doc stays greppable as it
grows — see CLAUDE.md and SESSION_EXAMPLE.

Exactly one session should be `active`. If `bin/sessions active` shows another
active session, surface it and ask whether to close it first (it likely needs
`/session-end`).

### 5. Mirror the checklist into tasks

If the task tool is available, create one task per checklist item via
`TaskCreate` so progress is tracked live in the session, and keep the session
doc's checkboxes and the task list in sync as work proceeds. (If it isn't, the
session doc's `[3] Checklist` remains the source of truth — no-op this step.)

### 6. Mark the in-scope tickets in-progress

For every ticket this session commits to working, set `status: in-progress` and
bump `updated:` to today (`/ticket` update, or edit the frontmatter directly).
This keeps the lifecycle gap-free (CLAUDE.md [4.1]) — `bin/tickets in-progress`
should equal the work actually in flight. Don't flip tickets you're only
referencing, and leave `future`/`icebox` ones as-is.

### 7. Refresh the live status + announce

Refresh the human-facing snapshot: `.claude/bin/status > .status.md` (gitignored;
the at-a-glance state for whoever is managing agents in this repo).

Point the user at the session doc path and give a tight summary:
the goal, the in-scope tickets, the key context pointers (notable research /
prior-session follow-ups), and the first 3 checklist items. Then start work (or
wait, if the user only wanted the session seeded).

## Quality bar

- **Context is specific, not generic.** "Reviewed tickets 0012–0014, ACs below"
  beats "looked at the backlog". Cite ids, paths, PR numbers.
- **The checklist is testable and TDD-ordered.** No "implement X" without a
  preceding "write failing test for X".
- **One active session at a time.** The session doc is central state; two active
  docs means ambiguous state.

## What NOT to do

- Don't allocate ids by hand-editing `.next-id` — use the script.
- Don't start writing code before the session doc exists — the doc is the
  central state, seed it first.
- Don't dump the entire backlog into Context — only what's relevant to the goal.

## Activity

After seeding the session doc, emit a feed event:

```bash
.claude/bin/activity session-start "Session · <goal>" "<slug>"
```
