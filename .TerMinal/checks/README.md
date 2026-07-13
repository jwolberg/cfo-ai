# .TerMinal/checks/

Repo-level **cadence inspection** artifacts, versioned with the code. Where
`.TerMinal/reviews/` holds per-PR reviews, `.TerMinal/checks/` holds whole-repo
hygiene scans (dead-code, dependency drift, …) run weekly/monthly or on demand
— **not** per-commit gates.

```
.TerMinal/checks/<kind>/<short_sha>.md     # one run per main-HEAD commit; newest-first by `generated`
```

- Each kind is defined by a contract at `.agents/<kind>.md` (see
  [`../../.agents/dead-code.md`](../../.agents/dead-code.md) — the example + pattern).
- Run with `/check <kind>` (e.g. `/check dead-code`). `/check` with no arg lists
  available kinds.
- **Advisory, not gating.** Checks report; they never delete code. Cleanup
  becomes a `/ticket` (usually `horizon: next`/`future`) → PR.
- Add a new kind: write `.agents/<newkind>.md` (copy the dead-code shape), then
  `/check <newkind>`.
