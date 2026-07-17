---
title: Daily summary — 2026-07-16
date: 2026-07-16
generated: agent (daily-summary)
---

# Daily summary — 2026-07-16

**The day in one line:** the multi-tenant persistence plan was written, filed as
tickets `0019`–`0026`, and built out — Postgres scoped by household with forced
RLS, a seeder, a scoped read path, a household switcher — and it ended with the
first Cloud Run revision in this repo's history whose `/health` check has ever
passed.

**Volume:** 44 commits (31 non-merge, 13 merges) · 13 PRs opened and merged
(#43–#55) · 90 files changed, +13,863 / −2,088 · 15 tickets opened, 12 closed.

> **Path note.** This agent's default output path is `.TerMinal/reports/`. This
> repo has no `.TerMinal/` tree — it keeps working docs under `docs/`
> (`docs/tickets/`, `docs/plans/`, `docs/reviews/`, `docs/learnings/`), so this
> digest is filed at `docs/reports/` to match. No harness directories were
> created.

---

## Merged PRs

All 13 opened, CI'd, and merged today. Chronological.

| PR | Title | Size |
|----|-------|------|
| [#43](https://github.com/jwolberg/cfo-ai/pull/43) | docs(persistence): Postgres scoped by household — plan, ADR-0004, tickets 0019-0026 | +1911/−5 |
| [#44](https://github.com/jwolberg/cfo-ai/pull/44) | fix(db): one walk, household-scoped Postgres, and the IDOR suite [0019-0021] | +2839/−180 |
| [#45](https://github.com/jwolberg/cfo-ai/pull/45) | docs(tickets): 0019-0022 done, 0023 blocked, 0026 in progress | +135/−24 |
| [#46](https://github.com/jwolberg/cfo-ai/pull/46) | fix(engine): a ledger per card, and APR provenance [0027, 0028] | +1217/−96 |
| [#47](https://github.com/jwolberg/cfo-ai/pull/47) | docs: the README is an app, not an engine repo | +207/−29 |
| [#48](https://github.com/jwolberg/cfo-ai/pull/48) | feat: the archetypes, the seeder, and a DayRecord that carries a portfolio [0023, 0030] | +2972/−735 |
| [#49](https://github.com/jwolberg/cfo-ai/pull/49) | feat: the read path and the household switcher [0024, 0025] | +1644/−170 |
| [#50](https://github.com/jwolberg/cfo-ai/pull/50) | fix(deploy): the container had no sqlalchemy — and 0031 waits for ingest [0031] | +371/−132 |
| [#51](https://github.com/jwolberg/cfo-ai/pull/51) | docs(tickets): 0032 — generate() is not prefix-stable, filed at last | +123/−0 |
| [#52](https://github.com/jwolberg/cfo-ai/pull/52) | feat(api): the spend surface, per household and per card | +2787/−1319 |
| [#53](https://github.com/jwolberg/cfo-ai/pull/53) | docs: architecture's summary, and a deploy command that dropped CORS | +50/−20 |
| [#54](https://github.com/jwolberg/cfo-ai/pull/54) | docs: the Neon credentials, the first real deploy, and five claims that were false | +315/−18 |
| [#55](https://github.com/jwolberg/cfo-ai/pull/55) | docs(readme): the API surface, and what the seeder's 150 days protect | +43/−0 |

**Landmark commits**

- [`45a7eea`](https://github.com/jwolberg/cfo-ai/commit/45a7eea) — household-scoped schema, forced RLS, partitioned decision log `[0020]`
- [`27a5ebe`](https://github.com/jwolberg/cfo-ai/commit/27a5ebe) — household-scoped repository and the IDOR suite `[0021]`
- [`da93594`](https://github.com/jwolberg/cfo-ai/commit/da93594) — refuse to start under a role that bypasses RLS `[0026 partial]`
- [`0b216d5`](https://github.com/jwolberg/cfo-ai/commit/0b216d5) — the archetypes, and the seeder `[0023]`
- [`8a7f3ff`](https://github.com/jwolberg/cfo-ai/commit/8a7f3ff) — the read path: households from Postgres, scoped `[0024]`
- [`619375e`](https://github.com/jwolberg/cfo-ai/commit/619375e) — the deploy ran; README corrected against measurement

---

## Tickets opened — 15 (`0019`–`0033`)

Filed from the multi-tenant persistence plan (`0019`–`0026`), plus seven spawned
mid-build. Spawned tickets are the norm here; seven in one day is on-trend, not
an anomaly.

**From the plan** ([#43](https://github.com/jwolberg/cfo-ai/pull/43), `057405e`)

- `0019` The walk, unified — and the dial nothing reads *(bug)*
- `0020` Schema, RLS, and the first migration *(feature)*
- `0021` Repository scoping and the IDOR suite *(feature)*
- `0022` The SnapshotStore seam *(feature)*
- `0023` The archetypes, and the seeder *(feature)*
- `0024` The read path — serve from Postgres, scoped by household *(feature)*
- `0025` Mobile — the household switcher *(feature)*
- `0026` Neon, and the deploy path *(chore)*

**Spawned during the build**

- `0027` A ledger per card — the walk cannot simulate a portfolio *(bug)*
- `0028` APR provenance — estimate at 23%, but never claim a saving from a guess *(feature)*
- `0029` The income bucket is biweekly-shaped — the gate refuses households whose income is regular *(bug)*
- `0030` DayRecord carries a portfolio — balance and APR per card *(feature)*
- `0031` The spend surface, per household — and for a portfolio *(feature)*
- `0032` generate() is not prefix-stable — the harness has never graded the household that ships *(bug)*
- `0033` A migration that imports live code is not a migration *(bug)*

---

## Tickets closed — 12

`0019` · `0020` · `0021` · `0022` · `0023` · `0024` · `0025` · `0027` · `0028` ·
`0030` · `0031` · `0033`

**Still open at end of day — 3**

| Ticket | Status | Why it matters |
|--------|--------|----------------|
| `0026` Neon, and the deploy path | in-progress | The deploy ran and the container starts; the ticket's remaining scope is the frontend half. |
| `0029` The income bucket is biweekly-shaped | **open** (bug, high) | The gate refuses households whose income is regular. |
| `0032` generate() is not prefix-stable | **open** (bug, high) | The harness has never graded the household that ships. |

Both open bugs are `priority: high` and both were filed today rather than fixed.

---

## Code-review verdicts — none

Reported as empty rather than padded:

- **0 GitHub review approvals** across #43–#55. Every PR merged without a formal
  review verdict.
- **Bugbot is disabled** for this account — it commented the same
  "not enabled … was not reviewed" upsell on 11 of the 13 PRs. No automated
  review ran on any PR today.
- `docs/reviews/` was untouched; its only artifact is
  [`2026-07-12-prd-adversarial-review.md`](../reviews/2026-07-12-prd-adversarial-review.md)
  from four days ago.

The substantive review of the day happened *inside* the work, not on the PRs —
see the corrections logged in `implementation-notes.md` below.

---

## Check artifacts — CI

**34 runs, 33 green, 1 red.** Every merged branch was green at merge.

- 13 runs on `main` — all success
- 21 runs across 13 feature branches — 20 success
- **1 failure:** [`CI` on `fix/unify-the-walk`](https://github.com/jwolberg/cfo-ai/actions/runs/29532526683)
  — diagnosed in [`5886eb5`](https://github.com/jwolberg/cfo-ai/commit/5886eb5)
  ("the CI flake was two bugs, and only one was a timeout") and fixed before the
  branch merged in #44.

**Test counts, measured today** (`619375e`): 532 Python tests collected (up from
448), 66 mobile (up from 51). Locally 393 pass / 139 skip / 0 fail — the DB
suites decline without `TEST_DATABASE_URL` by design. CI sets it and runs all 532.

---

## Agent runs — none recorded

The scheduled-agent ledger (`recent_agent_runs`, repo `cfo-ai`) returned empty.
No cron or scheduled agent ran against this repo today. The day's 44 commits were
interactive-session work.

---

## Two things worth your attention

**1. `main` is not protected — measured, not assumed.**
`GET /repos/jwolberg/cfo-ai/branches/main/protection` returns **404 Branch not
protected**. `docs/runbooks/branch-protection.md` is a setup runbook whose
checkboxes are all still unchecked, and the agent-side guard it references —
`.claude/hooks/block-main-merge.sh` — **does not exist** (`.claude/` contains only
an empty `skills/` directory). So neither the forge-side nor the agent-side guard
described in the runbook is actually in place. Nothing enforces the PR flow today
except convention — a convention that held 13/13 times.

**2. The day's recurring failure mode, in the team's own words.**
`implementation-notes.md` took 10 commits today, and four separate entries record
a confidently-written claim about system state turning out false. From `115de9c`:

> This is the fourth time in two sessions that a confidently-written claim about
> state turned out false, and the first one I wrote myself. The tell was identical
> every time: **it described what the code was designed to be rather than what a
> request returns.**

Both directions of the lesson got logged. From `619375e`, on two "bugs" that were
the checks' fault, not the product's:

> **when a check fails, suspect the check.**

Known-stale, called out in-notes and not yet fixed:
- `docs/RUNBOOK.md` §7's acceptance checks curl `/decisions` and `/spend` — routes the live service no longer serves.
- Version 1 of `resfi-database-url` is stale (direct host, no `+psycopg`, rotated password). Inert, but worth disabling.
- `.status.md` still reads `updated 2026-07-13` and `open 2 · in-progress 2` — it did not track today at all.

---

## Underlying artifacts

- Tickets — [`docs/tickets/`](../tickets/) (`0019`–`0033`)
- Plan — [`docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md`](../plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md)
- Brainstorm — [`docs/brainstorms/2026-07-16-multi-tenant-persistence-and-seeded-households.md`](../brainstorms/2026-07-16-multi-tenant-persistence-and-seeded-households.md)
- ADR — [`docs/decisions/0004-postgres-scoped-by-household.md`](../decisions/0004-postgres-scoped-by-household.md)
- Running notes — [`docs/implementation-notes.md`](../implementation-notes.md)
- Runbooks — [`deploy.md`](../runbooks/deploy.md) · [`neon-provisioning.md`](../runbooks/neon-provisioning.md) · [`local-development.md`](../runbooks/local-development.md)
- CI — [Actions](https://github.com/jwolberg/cfo-ai/actions)
