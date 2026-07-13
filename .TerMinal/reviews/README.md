# .TerMinal/reviews/

In-repo code-review artifacts, versioned with the code. `/code-review` (via
Codex) writes here; no central dashboard, no harness phone-home.

```
.TerMinal/reviews/<pr-number>/
  <short_sha>.md       # one combined review+tests artifact per reviewed commit
  findings.json        # canonical per-finding state (blocking review items)
  suggestions.json     # canonical per-suggestion state (non-blocking notes)
```

- **Filename = short SHA** makes "which commit was reviewed" explicit.
- Schema + scoring rubric + verdict logic live in
  [`../../.agents/code-review.md`](../../.agents/code-review.md); runner detection in
  [`../../.agents/testing.md`](../../.agents/testing.md).
- Merge bar: `verdict: approve` + `test_status: pass` + zero findings at
  severity ≥ medium. Overall score is informational, not a gate.
- `/code-review` can file out-of-scope follow-ups as backlog tickets
  (`horizon: future`) and references their ids in the artifact.
