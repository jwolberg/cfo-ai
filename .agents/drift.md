# drift-auditor agent (in-repo contract)

A scheduled agent that compares **what the docs claim** against **what the code
does**, surfacing drift in `architecture.md`, ADRs, runbooks, per-folder
`CLAUDE.md`, and the root project-level `CLAUDE.md` rules.

Unlike `auto-docs` (which *generates* tripartite content under
`docs/<category>/`), drift-auditor is a **report-then-propose** agent: it
identifies discrepancies between docs and reality and either files a ticket
(for shape drift / non-trivial decisions) or opens a small fix PR (for trivial
mismatches it can confidently correct).

**Workflow is uniform**: own worktree → analyze → propose changes via a PR /
file a ticket → human merges / triages.

## Mode

`writer` for *trivial* fixes (typos, renamed file refs, stale paths) — opens a
small PR. Otherwise `report` — files a ticket and writes the audit artifact only.

## Inputs

- `docs/architecture.md` + `CLAUDE.md` (root) + per-folder `CLAUDE.md` files.
- `docs/decisions/*.md` (ADRs) — check `status` field consistency.
- `docs/runbooks/*.md` — check `last-verified` freshness.
- Source tree (current state vs what docs reference).
- `git log <lastScannedSha>..HEAD` for the diff since the last run.

## Early-exit fast path

State at `~/.config/TerMinal/agent-state/<repo-basename>/drift.json`:

```json
{ "lastScannedSha": "abc1234", "lastRunAt": ..., "lastFindings": 3 }
```

If `HEAD == lastScannedSha`, exit 0.

If only changes since `lastScannedSha` are inside `docs/` or `CHANGELOG.md`
(no source changes), still scan (a doc-only edit could introduce drift) but
skip the source-vs-docs cross-check for speed.

## Findings catalog

The auditor categorizes each finding by **type** so the framework can decide
PR vs ticket:

| Type | Example | Decision |
|---|---|---|
| **broken-path** | `architecture.md` references `src/main/foo.ts` that no longer exists | PR (one-line fix) |
| **renamed-symbol** | A doc refers to `oldFunction()` but only `newFunction()` exists | PR (text substitution) |
| **stale-runbook** | A runbook's `last-verified:` is >90 days old | Ticket |
| **adr-contradiction** | An accepted ADR claims X but the code does Y | Ticket (high priority) |
| **claude-md-drift** | `CLAUDE.md` rule contradicts shipped code (e.g., "we use X" but code uses Y) | Ticket |
| **module-undocumented** | A new top-level src/ file/folder has no doc entry | Ticket (low priority) |

## Process

1. **Worktree**: `git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/drift-<short_sha>" main`.
2. **Scan** — walk every doc input above, for each claim extract referenced paths/symbols,
   verify against the source tree.
3. **Categorize findings** by the table above.
4. **For trivial-fix findings** (broken-path, renamed-symbol with unique
   substitution): apply the fix in the worktree.
5. **If any trivial fixes**: branch `drift/<short_sha>`, commit, push, open PR
   `chore(docs): fix N drift items`.
6. **For non-trivial findings**: file a ticket via `.claude/skills/ticket` with
   the audit-report path attached.
7. **Write artifact** to `.TerMinal/reports/drift/<short_sha>.md` (always — even if no
   findings, the artifact records the run).
8. **Update state** — `lastScannedSha = HEAD`, `lastFindings = N`.
9. **Activity** — `.claude/bin/activity check "Drift · <N> findings" "@ <short_sha>"`.

## Output artifact

`.TerMinal/reports/drift/<short_sha>.md`:

```yaml
---
kind: drift
generated: 2026-06-01T08:00:00Z
sha: abc1234
last_scanned: 9b3de89
findings:
  broken-path: 2
  renamed-symbol: 1
  stale-runbook: 0
  adr-contradiction: 0
  claude-md-drift: 1
  module-undocumented: 3
trivial_fix_pr: https://github.com/owner/repo/pull/N
tickets_filed: [.TerMinal/backlog/0124-drift-claude-md.md]
status: ok
---

# Drift audit · <short_sha>

## Trivial fixes (PR'd)
…

## Tickets filed
…

## All findings (raw)
…
```

## Hard rules

1. **No source code edits.** This agent's PRs only touch docs/markdown
   (specifically: paths referenced in `docs/`, root `CLAUDE.md`, per-folder
   `CLAUDE.md`). Non-doc fixes are tickets, not PRs.
2. **Ticket + MR workflow** — every change goes through a PR.
3. **Worktree isolation**.
4. **Idempotent** — re-running on the same HEAD is a no-op.
5. **Stale-runbook + ADR contradictions are HIGH priority tickets** — they
   indicate the workflow itself is drifting.
