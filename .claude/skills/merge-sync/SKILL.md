---
name: merge-sync
description: "Reconcile tickets/backlog with reality: close tickets whose PRs merged, scrub merged URLs, sweep status drift. Edits ticket files only, never merges. Use after merging a stack or whenever PRs have landed."
---

# /merge-sync — Reconcile tickets with merged PRs

The merge is human-only (global §8), so after the human merges, ticket state is
stale: tickets still say `in-progress` and still list the now-merged PR/MR in
`prs:`. This skill closes the loop. It **never merges** — it only reads PR/MR
state from the forge and updates the active ticket directory
(`.TerMinal/backlog/` in v2, `backlog/` in legacy v1).

## Process

### 1. Run the deterministic reconcile — `bin/merge-sync`

Steps 1–3 of the old manual flow (find tickets with linked PRs → ask the forge
what merged → scrub merged URLs + close fully-merged tickets) are **judgment-
free**, so a script does them in one forge call instead of N tickets × N `gh`
round-trips:

```bash
.claude/bin/merge-sync            # dry-run: print the reconcile plan (read-only)
.claude/bin/merge-sync --apply    # execute the closes + scrubs
```

It runs one `gh pr list --state merged` (or `glab mr list`, resolved via
`.claude/bin/forge`), matches against every ticket's `prs:`, and: closes a ticket
only when its linked PR is *actually* merged (sets `status: closed` + `updated:`
today), scrubs merged URLs (handles all `prs:` formats), and scrubs
`closed`-but-still-linked drift. A single PR closing multiple tickets is handled
naturally — every ticket listing that URL reconciles. **Only** edits ticket files.

Review the dry-run plan first; run `--apply` when it looks right. For tickets
that landed via a scheduled/`/bg` MR merge, also call
`set_run_outcome({runId: $TERMINAL_RUN_ID, outcome: 'merged'})` (MCP).

### 4. Sweep for drift (periodic cleanup)

Beyond merge-driven closes, do a quick hygiene pass so the backlog matches
reality (CLAUDE.md [4.1] — this is the workflow's periodic ticket cleanup, and
the same sweep `/session-end` runs):

- **`in-progress` with no open PR** and no active session on it → move back to
  `open`, or to `stuck` (note why) / `icebox`. Don't leave it falsely in flight.
- **`stuck` with a cleared blocker** (resolved HITL, closed dependency, available
  credential/approval, or original blocker no longer reproduces) → move back to
  `open`, or `in-progress` if resuming it now. Don't leave it falsely blocked.
- **`closed` but still listing a `prs:` URL** → scrub the URL.
- **Long-stale `open`** (untouched, clearly out of scope) → surface it and
  suggest `icebox`/`future`; change it only with confirmation, since `open` may
  just mean "not started yet".

### 5. Report

List what changed: which tickets were closed, which PR URLs were scrubbed, any
status drift fixed, and any ticket left open because not all its PRs merged. Refresh the live snapshot
(`.claude/bin/status > .status.md`) to reflect the closed tickets. Don't
auto-`/document` or file new tickets — this is pure reconciliation.

## When to run

- After merging a `/stacked-mr` batch in the morning (the common case — ~N PRs
  merged at once).
- At the top of `/session-start` (context-seeding can detect merged-but-open
  tickets and suggest running this).
- Anytime `gh pr list --state merged` shows recent merges not yet reflected in
  the backlog.

## Hard rules

1. **Never merge.** Read PR state only; `gh pr merge` is blocked by the hook.
2. **Only edit ticket files.** No code changes, no doc writes.
3. **Don't close a ticket whose PR isn't actually merged** — verify via `gh`,
   don't assume from the `prs:` link alone.

## Activity

For each ticket closed during reconciliation, emit a feed event:

```bash
.claude/bin/activity ticket-closed "Ticket closed · #<id>" "PR !<iid> merged" --ticket <id> --pr <iid>
```
