---
name: document
description: "Sweep recent changes and propose doc updates (ADRs, runbooks, learnings, architecture, per-folder CLAUDE.md), one candidate at a time. Use on /document, 'capture decisions', or natural post-commit/post-PR pauses."
---

# /document — Capture decisions and knowledge from this session

Walk recent changes and propose documentation updates. **One candidate at a time** — show draft, ask user to confirm/edit/skip, then move on.

## Where docs live

**Per-project `docs/`** (in repo root):
- `docs/decisions/NNNN-slug.md` — ADRs (append-only)
- `docs/architecture.md` — evergreen system overview (edit-in-place)
- `docs/runbooks/<task>.md` — repeatable ops procedures (edit-in-place)
- `docs/learnings/<topic>.md` — non-obvious findings (edit-in-place)

**Per-folder `<folder>/CLAUDE.md`** — purpose, conventions, gotchas, key entry points for that folder. Auto-loaded by Claude Code when working in that subtree.

**Global `~/.claude/docs/`** — same subfolder shape (`decisions/`, `runbooks/`, `learnings/`). For knowledge useful across *any* future project.

## What triggers what

| Trigger | Doc type |
|---|---|
| Non-obvious choice made (library, architecture, "we tried X going with Y") | ADR |
| New top-level folder, major dep, or service added | Architecture edit |
| Manual ops sequence completed that may recur | Runbook |
| Surprising finding ("X didn't work because Y", subtle invariant, gotcha) | Learning |
| New folder created without obvious purpose | Per-folder CLAUDE.md |

If the change is trivial (typo, formatting, dep bump with no decision behind it), no doc is needed. Don't fabricate candidates.

## Schemas (always write valid frontmatter)

### ADR

```yaml
---
id: NNNN              # zero-padded 4 digits, next free in docs/decisions/
title: <short title>
status: proposed | accepted | superseded | deprecated
date: YYYY-MM-DD
supersedes:           # optional, ADR id like 0003
superseded-by:        # optional, ADR id
---
```

Body sections (in order): `## Context`, `## Decision`, `## Consequences`.

### Runbook

```yaml
---
title: <task name>
last-verified: YYYY-MM-DD
---
```

### Learning

```yaml
---
title: <short title>
date: YYYY-MM-DD
tags: [tag1, tag2]
---
```

### Architecture & per-folder CLAUDE.md

No frontmatter. Freeform markdown.

## Process

1. Review session context: recent edits, commands run, decisions discussed in conversation, new files/folders created.
2. Build a list of candidate doc updates (type + summary). Keep it lean — only real candidates.
3. **Present one candidate at a time.** Show the proposed type, path, frontmatter, and body draft.
4. Ask user to **confirm / edit / skip**. Apply edits to the draft if requested.
5. On confirm: write the file. For ADRs, allocate the next free `NNNN` by listing `docs/decisions/`. If the repo uses git-sync (auto-commits on file changes), the existing flow handles the commit.
6. Move to next candidate. Stop when the list is exhausted or the user calls it.

## Per-project vs global routing

Per-project: specific to this codebase (decisions about this app, runbooks for this app's ops, learnings about its quirks).

Global (`~/.claude/docs/`): useful in *any* future project (cross-cutting patterns, learnings about a tool/library/language, generic procedures).

If unclear, ask the user once per candidate.

## What NOT to write

- **Session activity logs** ("today I changed X, Y, Z") — git log already captures what was done.
- **In-file comments** — project rules govern those separately; this skill is sidecar `.md` only.
- **Trivial changes** — typos, formatting, mechanical refactors with no decision behind them.
- **Speculative future plans** — those belong in a ticket/backlog, not docs.
- **Duplicate ADRs** — if a similar decision already exists, propose an edit (or a `superseded-by` link) instead of a new ADR.

## Activity

After a doc is written, emit a feed event:

```bash
.claude/bin/activity doc "Doc · <ADR/runbook/learning> written" "<title>"
```
