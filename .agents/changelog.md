# changelog agent (in-repo contract)

A scheduled agent that maintains **`CHANGELOG.md`** in [Keep a Changelog](https://keepachangelog.com)
style. The agent is the **sole writer** of `CHANGELOG.md` per `.agents/owned.yml`
— no other agent (factory, stacked-mr, drift-auditor) ever amends it.

**Workflow is uniform with every other scheduled agent**: spin its own worktree
→ analyze → propose changes via a PR → human merges. The "sole writer" label
means no parallel agent races; it does **not** mean the agent commits directly
to `main`.

## Mode

`writer` — opens a PR amending its owned paths.

## Inputs

- Merged commits since the agent's `lastScannedSha`.
- Each merged PR's title, body, labels, and linked tickets.
- Existing `CHANGELOG.md` (parse the top "Unreleased" section if present).

## Early-exit fast path

State at `~/.config/TerMinal/agent-state/<repo-basename>/changelog.json`:

```json
{ "lastScannedSha": "abc1234", "lastRunAt": 1700000000000, "lastEntryCount": 12 }
```

On run:
```bash
head=$(git rev-parse HEAD)
[ "$head" = "$lastScannedSha" ] && exit 0   # no new commits, no work
```

If `git log <lastScannedSha>..HEAD --merges` is empty, exit 0 (only direct
commits, no MRs to log).

## Process

1. **Worktree**: `git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/changelog-<short_sha>" main`.
2. **Walk new merges** — `git log <lastScannedSha>..HEAD --merges --format=...`.
   For each, resolve the source PR (via `gh`/`glab`) to get title + body + labels.
3. **Group by Conventional Commits type** — `feat:`, `fix:`, `docs:`, `refactor:`,
   `test:`, `chore:`. Map to Keep-a-Changelog sections:
   - `feat` → **Added**
   - `fix` → **Fixed**
   - `refactor` / `chore` → **Changed**
   - `docs`, `test` → **Maintenance** (kept if non-trivial)
4. **Update `CHANGELOG.md`**:
   - Insert entries under the top `## [Unreleased]` section, preserving header.
   - Each entry: `- <one-line summary> ([!<num>](<MR-url>))`.
   - Keep the **managed-by header** at the top of the file intact (see template).
5. **Open the PR** with `--base main` and title `chore(changelog): N new entries`.
   Body lists the merges included. Branch: `changelog/<short_sha>`.
6. **Update state** — write the new `lastScannedSha = HEAD` to the state file.
7. **Emit activity** — `.claude/bin/activity doc "Changelog · +N entries" "@ <short_sha>"`.

## Decisions

| Condition | Action |
|---|---|
| No new merges since `lastScannedSha` | Exit early, update state, no PR. |
| 1+ new merges | Open PR amending only `CHANGELOG.md`. |
| Merge with no Conventional Commits type | Bucket as **Changed**, include MR ref. |
| Parsing error on a merge body | Log to artifact, skip the entry, continue. |

## Output artifact

`.TerMinal/reports/changelog/<short_sha>.md` — frontmatter + body summarizing what the
run found (entries added, merges skipped, time taken). Newest-first by
frontmatter `generated`.

```yaml
---
kind: changelog
generated: 2026-06-01T08:00:00Z
sha: abc1234
last_scanned: 9b3de89
entries_added: 12
pr_opened: https://github.com/owner/repo/pull/N
status: ok
---
```

## Hard rules

1. **Sole writer of `CHANGELOG.md`** — never touch any other file.
2. **Ticket + MR workflow** — never push directly to `main` (the merge-block hook
   enforces this).
3. **Worktree isolation** — every run gets its own worktree (parallel-safe).
4. **Idempotent** — re-running on the same head SHA must be a fast no-op.

## Managed-by header for CHANGELOG.md

The agent inserts/preserves this at the top of the file:

```markdown
<!-- managed by: changelog · do not edit by hand · see .agents/changelog.md -->
```
