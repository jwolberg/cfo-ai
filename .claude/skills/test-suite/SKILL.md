---
name: test-suite
description: "Delegate an ad-hoc test run to Codex and report results in chat (no artifact) — the cheap inner-loop step; the code-review agent is the checkpoint. Use on /test-suite or 'run the tests'."
---

# /test-suite — Run tests, report in chat

Ad-hoc test runs. Detects the runner per [`.agents/testing.md`](../../../.agents/testing.md),
runs the suite once, and reports results in chat. **Writes no artifact.**

This is the **cheap inner-loop step**: run it between commits while iterating to
confirm tests stay green, then run the `code-review` agent at a checkpoint for the full
six-axis review (which runs tests itself as its gate and embeds the result in
the `.reviews/` artifact). Use `/test-suite` when you want to know whether tests
pass *without* paying for a full review.

## Delegate to Codex

```bash
codex exec -C "$PWD" "/test-suite"
```

Run directly in Claude only if `codex` is unavailable or the user asks.

## Process

1. **Detect the runner** per `.agents/testing.md` (package.json scripts.test →
   bun test/vitest/jest; pyproject → pytest; Cargo.toml → cargo test; go.mod →
   go test ./...; etc.). If multiple, prefer the one CI runs.
2. **Install deps if needed** (only if unresolved — no node_modules/.venv).
   Prefer `bun install` per global §5.
3. **Run once** — capture stdout+stderr, exit code, wall time. No retries.
4. **Report in chat:**

```
**Runner:** <bun test / pytest / ...>
**Command:** <exact command>
**Status:** pass | fail | partial | error
**Counts:** N passed, N failed, N skipped (total N)
**Duration:** X.Xs

<one-line summary; on failure, list test name + key error excerpt>
```

## What NOT to do

- **Don't write artifact files.** That's the `code-review` agent's job (the combined
  `.reviews/` artifact).
- **Don't edit test files to make them pass.** Failing tests become findings.
- **Don't mark `pass` when tests were skipped due to install failures** — that's
  `error`.
- **Don't re-run until green.** One run per invocation; flaky tests are findings
  worth a `/ticket`.

## Activity

After the run, emit a feed event with the result:

```bash
# pass:
.claude/bin/activity tests-pass "Tests pass · <N> passed" "<runner> in <time>"
# fail:
.claude/bin/activity tests-fail "Tests fail · <F> failed / <N>" "<runner>"
```
