# perf agent (in-repo contract)

A scheduled agent that runs a **project-defined benchmark**, compares results
to a stored baseline, and files a ticket if there's a regression beyond
threshold. Opt-in: only runs if the repo defines a benchmark script.

**Workflow is uniform**: own worktree → benchmark → propose ticket / fix-attempt PR.

## Mode

`report` by default; `writer` for opt-in fix-attempt PRs when the cause is
clearly traceable (e.g., a known-pattern regression in a single file).

## Inputs

- Project benchmark script. Discovery order:
  - `package.json` `scripts.bench`
  - `bench/` directory with a runnable suite
  - `Cargo.toml` `[[bench]]` entries
- `.agents/perf/baseline.json` — last-known-good results per benchmark.
- `git log <lastScannedSha>..HEAD` to attribute the regression.

## Early-exit fast path

State at `~/.config/TerMinal/agent-state/<host>/<repo>/perf.json`:

```json
{ "lastScannedSha": "abc1234", "lastRunAt": ..., "regression": false }
```

If `HEAD == lastScannedSha` → exit 0.

If the repo has **no benchmark script**, write a single artifact
`status: not-configured` and exit (don't fail; just note for the operator).

## Process

1. **Worktree**: `git worktree add "${WORKTREES_DIR:-$HOME/.worktrees}/<repo>/perf-<short_sha>" main`.
2. **Run the benchmark** — single pass, with the project's own runner. Warm-up
   per the runner's convention.
3. **Compare to baseline** — per-benchmark, compute % delta. Regression
   threshold default 10%, configurable in `.agents/perf/config.json`.
4. **Identify likely culprit** — `git log <lastScannedSha>..HEAD` filtered to
   files touched by the regressing benchmark's code path.
5. **Decide**:
   - Regression < threshold → no action; update baseline only if improvement.
   - Regression ≥ threshold + single suspect commit → file a ticket with the
     commit, the benchmark diff, and a one-line hypothesis.
   - Regression with a clear-fix pattern (e.g., a known anti-pattern reverted
     to inefficient form) → opt-in fix-attempt PR. Default off.
6. **Write artifact** to `.TerMinal/reports/perf/<short_sha>.md`.
7. **Update state** + (if improvement and stable) baseline.
8. **Activity** — `.claude/bin/activity check "Perf · <regressions> regressions · <improvements> wins" "@ <short_sha>"`.

## Output artifact

`.TerMinal/reports/perf/<short_sha>.md`:

```yaml
---
kind: perf
generated: 2026-06-01T08:00:00Z
sha: abc1234
last_scanned: 9b3de89
benchmarks_run: 12
regressions:
  - name: "fibonacci-cps"
    delta_pct: -18.4
    suspect: "abc1234 — feat: refactor recursion to iterative"
improvements: 2
tickets_filed: [.TerMinal/backlog/0127-perf-fibonacci.md]
status: ok
---
```

## Hard rules

1. **Single run per invocation** — no retries-to-green. Variance is noted, not
   smoothed.
2. **Never edit benchmark code** to "fix" a regression — that's a ticket.
3. **Ticket + MR workflow** — every change goes through a PR.
4. **Worktree isolation**.
5. **Idempotent.**
6. **No-config = no-fail.** If the repo doesn't define benches, log
   `not-configured` and exit cleanly.
