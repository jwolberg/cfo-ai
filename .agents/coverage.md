# coverage agent (in-repo contract)

A scheduled agent that finds **test coverage gaps** and **CI flakes**, then
either opens a PR adding tests (for clear gaps it can confidently fill) or
files a ticket (for big surfaces / flaky tests needing investigation).

**Workflow is uniform**: own worktree → analyze → propose PR / ticket → human merges.

## Mode

`writer` — opens PRs with net-new test files. Source-under-test is never
modified (tests-only PRs).

## Inputs

- Test runner detection per `.agents/testing.md`.
- Coverage report from the runner (`--coverage`, `c8`, `nyc`, `pytest-cov`, etc.).
- `.agents/coverage/baseline.json` — last week's coverage numbers per file.
- CI run history (last N runs) for flake detection — via `gh run list` / `glab
  ci status` parsed for retried jobs.

## Early-exit fast path

State at `~/.config/TerMinal/agent-state/<repo-basename>/coverage.json`:

```json
{ "lastScannedSha": "abc1234", "lastRunAt": ..., "lastCoveragePct": 78.4, "flakeCount": 0 }
```

If `HEAD == lastScannedSha` AND no new CI runs since `lastRunAt` → exit 0.

## Process

1. **Worktree**: `git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/coverage-<short_sha>" main`.
2. **Run the test suite with coverage** (per `.agents/testing.md`). If tests
   don't pass, exit early with `status: blocked` (don't add tests to a broken suite).
3. **Identify gaps**:
   - Files with coverage drop since baseline (regression).
   - Files below the project's threshold (default 70%, configurable in
     `.agents/coverage/config.json`).
   - Functions with zero coverage in changed files (`git diff <lastScannedSha>..HEAD`).
4. **Scan CI for flakes** — parse the last N CI runs; tests that failed then
   passed on retry within the same run are flakes.
5. **Decide per finding**:
   - Small, well-bounded gap (single function, clear behavior) → write the
     failing test in the worktree.
   - Big surface / unclear contract → file a ticket.
   - Each flake → file a ticket with the failing seed + retry log.
6. **If any net-new tests written**: branch `coverage/<short_sha>`, commit,
   push, open PR `test: backfill <N> tests in <area>`.
7. **Write artifact** to `.TerMinal/reports/coverage/<short_sha>.md`.
8. **Update state** — `lastScannedSha`, `lastCoveragePct`, `flakeCount`.
9. **Activity** — `.claude/bin/activity check "Coverage · <pct>% (Δ <delta>) · <N> flakes" "@ <short_sha>"`.

## Output artifact

`.TerMinal/reports/coverage/<short_sha>.md`:

```yaml
---
kind: coverage
generated: 2026-06-01T08:00:00Z
sha: abc1234
last_scanned: 9b3de89
total_coverage_pct: 78.4
delta_pct: +1.2
files_below_threshold: 5
new_tests_pr: https://github.com/owner/repo/pull/N
flakes_detected: 2
tickets_filed: [.TerMinal/backlog/0125-flake-mr-checker.md]
status: ok
---
```

## Hard rules

1. **Tests-only PRs.** Never modify source under test in the same PR. If a fix
   is needed, file a separate ticket.
2. **Ticket + MR workflow** — every PR through human merge.
3. **Worktree isolation**.
4. **Idempotent** — re-running on same HEAD + same CI state is a no-op.
5. **Never tune the threshold to "pass."** Real gap → file a ticket; don't
   lower the bar.
