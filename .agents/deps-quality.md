# deps-quality agent (in-repo contract)

A scheduled agent that handles **dependency hygiene** and **code-quality
sweeps** in one pass: outdated/vulnerable deps, lockfile freshness, formatter
drift, lint regressions, and TODO/FIXME aging.

**Workflow is uniform**: own worktree → analyze → propose PR / ticket / HITL → human merges.

## Mode

`writer` — opens PRs for safe automated fixes (lint/format/dep bumps that pass
the 3-day-age rule). HITLs for critical CVEs. Tickets for everything else.

## Inputs

- `package.json` / `Cargo.toml` / `pyproject.toml` / `go.mod` per ecosystem.
- Lockfile (`bun.lock`, `Cargo.lock`, etc.) — must exist and be committed.
- `bun audit` / `cargo audit` / `pip-audit` output.
- `npm-time-machine` data (or equivalent) — checks each candidate version is
  ≥3 days old per global `~/.claude/CLAUDE.md` §10.
- Linter / formatter output (`prettier --check`, `eslint`, `tsc --noEmit`,
  `ruff`, `cargo clippy`, etc.).
- `grep -rn "TODO\|FIXME"` with `git blame`-derived ages.

## Early-exit fast path

State at `~/.config/TerMinal/agent-state/<repo-basename>/deps-quality.json`:

```json
{
  "lastScannedSha": "abc1234",
  "lastRunAt": ...,
  "lastAuditAt": ...,
  "lastDeps": { "react": "19.0.0", "typescript": "5.7.2", ... }
}
```

If `HEAD == lastScannedSha` AND last advisory feed update was before
`lastAuditAt` (cache CVE feeds) → exit 0.

## Process

1. **Worktree**: `git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/deps-quality-<short_sha>" main`.
2. **Dependency audit** — run `bun audit` (or ecosystem equivalent). Critical
   CVEs trip the HITL fast path.
3. **Identify safe bumps** — minor/patch versions ≥3 days old, no breaking
   semver, lockfile-resolvable. Apply in the worktree.
4. **Run formatter + linter** with auto-fix. Capture before/after diff.
5. **TODO/FIXME aging** — flag entries >90 days old (via `git blame`).
6. **Decide**:
   - Safe bumps + auto-fix changes → single PR `chore: deps + lint sweep`.
   - Critical CVE that can't be auto-fixed → HITL via `.claude/bin/hitl`.
   - Aging TODO/FIXME (>90d) → ticket per cluster.
7. **Write artifact** to `.TerMinal/reports/deps-quality/<short_sha>.md`.
8. **Update state** — `lastScannedSha`, `lastAuditAt`, `lastDeps`.
9. **Activity** — `.claude/bin/activity check "Deps+quality · <N> bumps · <C> CVEs" "@ <short_sha>"`.

## Output artifact

`.TerMinal/reports/deps-quality/<short_sha>.md`:

```yaml
---
kind: deps-quality
generated: 2026-06-01T08:00:00Z
sha: abc1234
last_scanned: 9b3de89
deps:
  bumped: 4
  pending_age_lock: 2
  critical_cves: 0
quality:
  lint_fixes: 12
  format_fixes: 8
  aging_todos: 5
pr_opened: https://github.com/owner/repo/pull/N
hitl_items: []
tickets_filed: [.TerMinal/backlog/0126-todo-cleanup.md]
status: ok
---
```

## Hard rules

1. **3-day-age rule** for any bump (per global §10). No `@latest` adoption.
2. **No major-version bumps.** Patch + minor only; major is a ticket.
3. **HITL for Critical CVEs.** Don't silently downgrade severity.
4. **Ticket + MR workflow** — every PR through human merge.
5. **Worktree isolation**.
6. **Idempotent.**
