# dead-code check (in-repo contract)

The example **cadence check** — a repo-level inspection run on a schedule (weekly/
monthly) or on demand, NOT per-commit. Reports unused code; never deletes it
(global §3 — removal is a ticket/PR decision, not an automated edit).

This file doubles as the **pattern** for adding new check kinds: copy its shape
to `.agents/<kind>.md`, then run it with `/check <kind>`. (Contrast with
`code-review.md`/`testing.md`, which are per-PR contracts, not cadence checks.)

## Inputs

- `repo_path` — the repo at its `main` HEAD (checks are repo-level, not PR-level).

## Tool detection (by ecosystem)

| Ecosystem | Preferred tool | Command |
|---|---|---|
| TS / JS | knip | `bunx knip --reporter json` |
| TS / JS (fallback) | ts-prune | `bunx ts-prune` |
| Python | vulture | `vulture <pkg> --min-confidence 80` |
| Rust | cargo-udeps / warnings | `cargo +nightly udeps` (deps) + `cargo build` dead-code warnings |
| Go | deadcode | `deadcode ./...` (golang.org/x/tools) |

If no tool is installed, note it in the artifact (`status: error`) and suggest
the install — don't fail silently or pretend a non-run is clean.

## Output location

```
.TerMinal/reports/dead-code/<short_sha>.md     # one per run, filename = main HEAD short sha
```

Newest-first by the frontmatter `generated` timestamp.

## Frontmatter

```yaml
---
kind: dead-code
commit: <full 40-char main HEAD sha>
short_sha: <7-char>
generated: <ISO 8601>
generator: <stable id>        # e.g. codex:gpt-5
tool: <knip | ts-prune | vulture | cargo-udeps | deadcode>
status: ok | findings | error
counts:
  unused_files: <int>
  unused_exports: <int>
  unused_deps: <int>
---
```

## Body

```markdown
## Summary

<1-2 lines: tool run, totals, overall read. On a clean run, one line.>

## Findings

Grouped by file. Each: `path:line` · what's unused (file / export / dep) ·
confidence. Dead code introduced by a recent PR is higher-signal than long-dead
code — note which is which when the tool can tell.

## Suggested cleanup

<Only if findings warrant action: the smallest set of tickets to file (via
/ticket, type: dx or testing, horizon usually next/future) to remove the dead
code. Do NOT delete here — checks report, humans/PRs act.>
```

## Hard rules

- **Report only.** Never delete code or edit source — that's a follow-up PR.
- **One run per invocation.** No retries.
- **Confidence matters.** Reflection/dynamic-dispatch/entrypoints often look
  "unused" to static tools — mark low confidence rather than asserting dead.
