---
name: stacked-mr
description: "Autonomous AFK mode: build a stack of owner-scoped PRs/MRs, then batch-review them to the bar. Human merges. Use on /stacked-mr or 'stack PRs'."
---

# /stacked-mr — Stacked PR/MR Pass

Builds a stack of PRs/MRs without reviewing each one immediately, then runs a
batch review pass at the end. The canonical owner, knowledge, artifact, and
follow-up contract is
[`docs/workflow/agent-process.md`](../../../docs/workflow/agent-process.md).

## Stack Shape

```text
main
  -> 0012-first-ticket       PR #1 base: main
       -> 0013-second-ticket PR #2 base: 0012-first-ticket
            -> 0014-third    PR #3 base: 0013-second-ticket
```

Each PR/MR diff is against its parent branch. The human merges bottom-up.

## Phase 1 — Build Stack

For each runnable ticket:

1. Pick the next `now` / `next` ticket by priority within the requested scope.
2. Confirm exactly one owner agent. If work needs multiple owners, split into
   linked tickets before building.
3. Set status `in-progress`.
4. Run the knowledge phase from `docs/workflow/agent-process.md`.
5. Branch from the current stack tip.
6. Implement TDD-first and commit.
7. File owner-scoped follow-up tickets for deferred work.
8. Run local smoke checks and inspect the diff.
9. Push the feature branch.
10. Open a PR/MR with base set to the parent branch.
11. Link the PR/MR URL into the ticket and update the stack ledger.

Do not run the `code-review` agent during this phase except for explicit
trust-critical escape hatches.

## Phase 2 — Batch Review

1. Refresh forge state for every PR/MR in the ledger.
2. Run one `code-review` agent pass per PR/MR in isolated worktrees.
3. Collect verdicts and review artifacts.
4. Fix `request-changes` branches, re-review only affected PRs/MRs, and restack
   children with `--force-with-lease` to feature branches only.

## Continue Conditions

- Runnable scoped tickets remain: keep building.
- True human-only blocker: file HITL and continue another lane.
- PR/MR cannot reach the bar after reasonable cycles: mark the ticket `stuck`
  with the artifact path and continue independent work.
- Scoped backlog exhausted: finish batch review and summarize.

## Summary Format

```text
Stack of N PRs/MRs, merge bottom-up:
  #1  0012-first-ticket   base: main              approve / tests pass
  #2  0013-second-ticket  base: 0012-first-ticket request-changes
```

Include PR/MR URLs, ticket ids, owners, bases, verdicts, test status, HITL
items, delegated artifact paths, and follow-up tickets.

## Hard Rules

1. Never merge.
2. Every PR/MR reaches the review bar before final handoff.
3. Force-push only feature branches and only with `--force-with-lease`.
4. TDD-first for every implementation ticket.
5. Batch reviews run in isolated worktrees.
