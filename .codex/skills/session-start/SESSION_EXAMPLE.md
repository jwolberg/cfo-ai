---
id: 0
slug: example-session
anchor: SES-0000
title: "Example session — copy this schema for new sessions"
status: active
started: 2026-01-01T09:00:00Z
ended: null
goal: "The verbatim /session-start argument goes here"
tickets: []
branches: []
prs: []
related_research: []
related_docs: []
prior_sessions: []
---

Canonical schema reference for a session's live doc. This file is **not** a real
session (the `bin/sessions` lister only matches
`.TerMinal/sessions/NNNN-slug/session.md` or legacy
`sessions/NNNN-slug/session.md`, and a `0` id makes it inert). Real session
docs live at `.TerMinal/sessions/NNNN-slug/session.md`. The directory may also
hold scratch files (generated checklists, intermediate analysis) alongside
`session.md`.

## Frontmatter fields (all required; arrays may be empty `[]`)

- `id` — integer, matches the numeric prefix of the session directory.
- `slug` — kebab-case, matches the directory suffix.
- `anchor` — the doc's anchor code, `SES-<zero-padded id>` (e.g. `SES-0007`).
  Used for cross-doc references like `SES-0007#3.1` (see the anchoring
  convention in `CLAUDE.md`). Every heading below carries a `[N]` / `[N.M]`
  anchor so any part of this (often long) doc is greppable.
- `title` — short headline derived from the goal.
- `status` — `active` | `closed` | `abandoned`. Exactly one session should be
  `active` at a time.
- `started` / `ended` — ISO 8601 datetimes. `ended` is `null` until closed.
- `goal` — the verbatim argument passed to `/session-start`.
- `tickets` — backlog ticket ids in scope this session (e.g. `[42, 43]`).
- `branches` — feature branches created/touched.
- `prs` — PR URLs opened or advanced this session.
- `related_research` — repo paths to research/notes consulted (seeded at start).
- `related_docs` — `docs/` paths (ADRs, runbooks, learnings) relevant or touched.
- `prior_sessions` — ids of earlier sessions this one continues or relates to.

## Body sections (strict order; keep all headings even if a section is brief)

In a real `session.md` these are top-level `##` headings, each carrying a `[N]`
**anchor** (subsections get `[N.M]`) so any part of this long doc is greppable —
`grep -n "\[3.1\]" session.md`, or reference it from elsewhere as `SES-0007#3.1`.
Append new subsection numbers; don't renumber existing ones (anchors are stable
ids, not ordinals). Shown below as they appear in the live doc:

```markdown
## [1] Goal

The goal expanded into 1–3 sentences: what we're trying to accomplish this
session and what "done" looks like.

## [2] Context & pointers

Seeded by /session-start. The map of everything relevant before work begins:
in-scope tickets (with one-line summaries + acceptance criteria), relevant
research/docs, existing tools/skills that apply, prior sessions and their
unfinished follow-ups, and the current git/PR state. Anchor each pointer group
as a subsection ([2.1] Tickets, [2.2] Research & docs, [2.3] Prior sessions,
[2.4] Git/PR state) so they're individually greppable.

## [3] Checklist

Seeded by /session-start, living through the session. Concrete, ordered TODOs —
**TDD-first**: every behavior change is a "write the failing test" item before
its implementation item. Anchor per ticket:

### [3.1] Ticket 0042 — rate-limit POST /join
- [ ] write failing test for rate-limit on POST /join
- [ ] implement rate-limit middleware to make it pass
- [ ] open PR + link the PR url into ticket 0042 prs:

## [4] Log

Append-only running notes. Anchor each entry with a timestamp subsection:

### [4.1] 2026-01-01T10:12Z — blocked on signer creds
... the "why" and the turns, not every command.

## [5] Decisions

Decisions made this session + reasoning. Each is an ADR candidate; /session-end
proposes promoting the load-bearing ones to docs/decisions/.

## [6] Outcomes

Filled by /session-end. What shipped: commits, branches, PR urls, tickets moved
to closed. The factual record of the session's output.

## [7] Follow-ups

Filled by /session-end. Deferred work, discovered bugs, known gaps, and
suggested new tickets (with ids once filed, usually horizon: future or next).

## [8] Documentation

Filled by /session-end. What got documented this session (ADR / learning /
runbook / architecture edits, with paths) and what still needs documenting.
```
