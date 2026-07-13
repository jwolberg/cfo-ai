# Reports

Structured, dated outputs from the scheduled-agent surface (`/check <kind>` +
`.agents/<kind>.md`). One markdown file per run, organized by agent kind:

```
.TerMinal/reports/
  changelog/<short_sha>.md
  drift/<short_sha>.md
  coverage/<short_sha>.md
  deps-quality/<short_sha>.md
  perf/<short_sha>.md
  health/<short_sha>.md
  auto-docs/<short_sha>.md
  dead-code/<short_sha>.md
  …
```

Each report has YAML frontmatter recording the run (kind, generated, sha,
last_scanned, key counts, status, PR/ticket/HITL outcomes) plus an optional
markdown body with the detail an operator needs to act on findings.

The **TerMinal Docs tab** surfaces this folder under its "Reports" category,
grouped by agent — so you read reports the same way you read maintainer /
developer / personal docs.

## Why these are checked in

History. Committing reports gives you a diff-able trend of drift,
coverage, deps health, perf, etc. over time. Per-run files are small
markdown; git handles them efficiently. If you'd rather keep them local-only,
add `.TerMinal/reports/` to `.gitignore` — the Docs tab still renders them.

## Who writes here

Each report kind is owned by its agent per `.agents/owned.yml`:

- `.TerMinal/reports/changelog/`   → `changelog`
- `.TerMinal/reports/auto-docs/`   → `auto-docs`
- `.TerMinal/reports/drift/`       → `drift-auditor`
- `.TerMinal/reports/coverage/`    → `coverage`
- `.TerMinal/reports/deps-quality/` → `deps-quality`
- `.TerMinal/reports/perf/`        → `perf`
- `.TerMinal/reports/health/`      → `health`
- `.TerMinal/reports/dead-code/`   → `dead-code` (legacy `/check` kind)

Other agents (factory, stacked-mr, etc.) **never** write here — that's the
sole-writer pledge.
