---
name: session-end
description: "Close a work session: reconcile the live session doc, clean up dead code/branches, run a refactor pass, file follow-up tickets, capture docs, mark closed. Use on /session-end or 'wrapping up'."
---

# /session-end — Close a session, leave the repo + docs clean

## Fast path: TerMinal MCP tools

When the `terminal-harness` MCP server is registered:

- **`update_ticket({slug, status: 'closed'})`** for shipped tickets.
- **`update_ticket({slug, removePrUrl: '<merged URL>'})`** when scrubbing
  merged PR URLs from open tickets.
- **`file_hitl({title, action, source: 'agent'})`** for open questions /
  blockers that surfaced.
- **`emit_activity({kind: 'session-end', title: '<outcome>', repo})`** at exit.

The reconciliation / quality-pass / docs-candidates / follow-up-tickets
playbook below stays — that's 95% the value of this skill.

---

Bookend to `/session-start`. Brings the active session doc + repo to a
clean, well-documented resting state. The single most important output
is **proper documentation** — nothing learned this session is lost.

**Not a quick wrap.** Often **15–30 min of real work** — cleanups, small
refactors, consistency fixes, ticket filing, doc writes. Work through
the steps until the repo and docs are genuinely clean.

## Process

### 1. Find the active session

```bash
ROOT="$(git rev-parse --show-toplevel)"
"$ROOT/.claude/skills/session-start/bin/sessions" active
```

Open its `sessions/<id>-<slug>/session.md`. If none is active, ask the user
which session to close. If more than one is active, surface them and ask.
In v2 repos the path is `.TerMinal/sessions/<id>-<slug>/session.md`; legacy v1
repos may still use `sessions/<id>-<slug>/session.md`.

### 2. Reconcile what happened → update the doc

Compare the session's plan to reality:

- `git log --oneline <started-or-base>..HEAD` and `git diff --stat` for the
  session's commits/branches.
- `gh pr list --author @me` / the branches in frontmatter for PRs opened or
  advanced.
- Walk the **Checklist**: tick done items; leave undone ones for Follow-ups.

Fill the session doc:
- **Outcomes** — commits, branches, PRs (urls), tickets moved to `closed`.
- **Decisions** — decisions made this session + reasoning (ADR candidates).
- **Log** — backfill any meaningful turns/blockers not already noted.
- Update frontmatter `branches` / `prs` if new ones appeared.

### 3. TDD + test verification pass

Verify TDD held:
- Every behavior change has a test. `/test-suite` green at HEAD.
- **Tests are adversarial, not rigged.** Spot-check that the session's
  new tests pin meaningful behavior — no tautologies, weak assertions,
  or over-mocking that would pass even if broken. See
  `.agents/testing.md` "Test quality".
- **Features are wired** (tests passing ≠ shipped). Each shipped feature
  is reachable from a real production entry point, ideally proven by
  automated e2e/integration. A symbol reachable only from tests is
  unwired.
- Gaps → file a `/ticket` (testing type) and note under Follow-ups.

### 4. Cleanup pass

Leave the tree cleaner than you found it — but **only your own mess** (global
§3). Identify and handle:
- Dead code introduced this session (unreachable branches, unused exports,
  commented-out blocks, debug logging) → remove.
- Scratch files in the session dir or repo that aren't worth keeping → remove.
- Stray `TODO`/`FIXME` added this session without a linked ticket → either fix
  or file a `/ticket` and reference it.
- Merged/abandoned local branches from this session → offer to prune.

Pre-existing dead code you didn't create: **mention it, don't delete it** (file
a ticket if worth it).

### 5. Code-quality / refactor pass

Scan this session's diff for quality issues (the anti-slop checklist in
`.agents/code-review.md`): speculative flexibility, over-abstraction, vague
names, WHAT-comments, unnecessary error handling. Fix small/local ones now;
for anything larger than a quick cleanup, file a refactor `/ticket` rather than
expanding scope at session end. If open PRs from this session haven't been
reviewed, recommend running the `code-review` agent on them (background — it's the
dev-speed bottleneck; ~4 min).

### 6. Consistency check (architecture + conventions)

Verify changes are consistent with the documented design — drift is how
codebases rot silently:

- **`docs/architecture.md`** — does code still match? Reconcile by fixing
  the code back to design OR updating `architecture.md` (and recording
  *why* as an ADR). Never leave them contradicting.
- **ADRs** — did anything contradict an `accepted` ADR? Supersede (new
  ADR with `supersedes:`) rather than silently diverging.
- **CLAUDE.md conventions** (root + nested). Fix divergences or file a
  `/ticket`.
- **Cross-file coherence** — terminology/patterns match the rest of the
  codebase.

Run `/document-audit` if docs were touched heavily.

### 7. Reconcile ticket statuses (periodic cleanup)

Make the backlog match reality — this is the workflow's **periodic ticket
cleanup** (CLAUDE.md [4.1]). Run `/merge-sync` first (closes any ticket whose
PR/MR has merged + scrubs the URL), then sweep `bin/tickets`:

- **Touched this session:** every ticket worked is `in-progress` (still WIP) or
  `closed` (merged) — never left `open`.
- **Merged but still open/in-progress:** close it (or let `/merge-sync` do it).
- **`in-progress` with no open PR and no active work:** move back to `open`, or
  to `stuck` (with a why) / `icebox` — don't leave it falsely "in flight".
- **Long-stale `open`** (untouched, out of scope): `icebox` or `future`, or
  confirm it's still wanted.

The bar: after this step, `bin/tickets in-progress` is exactly the work actually
in flight. Note any status changes in the session doc's Outcomes / Follow-ups.

### 8. Suggest new tickets

For every follow-up, discovered bug, deferred item, or refactor: file a
`/ticket` (present them **one at a time** and confirm — don't batch-dump).
Before filing, run `.claude/bin/list-agents` or MCP `list_agents({repo})` and
assign exactly one owner (`agent_id`, `agent_scope`, `agent_kind`). If a
follow-up needs multiple agents/phases, split it into linked tickets with
`depends_on`. Record the resulting ids under the session doc's **Follow-ups**.

### 9. Capture documentation (most important)

Decide what this session produced that's worth preserving, and route each via
`/document` (one candidate at a time):
- **Non-obvious decision** → ADR in `docs/decisions/`.
- **Surprising finding / subtle invariant / gotcha** → learning in
  `docs/learnings/`.
- **Repeatable manual ops sequence** → runbook in `docs/runbooks/`.
- **Structural change** (new folder, major dep, service) → edit
  `docs/architecture.md`; add a per-folder `CLAUDE.md` for new folders.

Then fill the session doc's **Documentation** section: what was captured (with
paths) and what still needs documenting (as a follow-up).

### 10. Close the session

Set `status: closed`, `ended:` now. Confirm every anchored body section
(`[1]`–`[8]`) is filled (no stub headings left) and any new subsections you
added carry `[N.M]` anchors. Refresh the live snapshot
(`.claude/bin/status > .status.md`) so it shows no active session. Commit the
session doc + any doc/cleanup changes on a feature branch (never main).

### 11. Summarize

A tight wrap-up: what shipped (PRs/tickets), what was cleaned, what was
documented (paths), and the top follow-ups for next session.

## Quality bar

- **Nothing learned is lost** — can point to where it was written down.
- **Repo is cleaner**, not just bigger.
- **Follow-ups are real tickets** with acceptance criteria, not vibes.

## What NOT to do

- Don't delete pre-existing code you didn't write (global §3).
- Don't merge to main (global §8).
- Don't batch-dump tickets — one at a time, confirm each.
- Don't leave `active` with stub sections — finish or mark `abandoned`.

## Activity

After closing the session doc, emit a feed event:

```bash
.claude/bin/activity session-end "Session closed · <slug>" "<one-line summary>"
```
