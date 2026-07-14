---
id: "0001"
title: Household decision-history precompute
type: feature
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md
depends_on: []
created: 2026-07-13
---

# Household decision-history precompute

Implements **U1** of the plan. Single owner: `backend-python-agent` (repo-scoped,
classic/one-shot — this is a foundational, non-recurring build step).

**Goal:** Generate the demo household's full decision history — debt ledger (with
interest accrual and `swept_this_week` tracking), derived `CashEvent`s, rolling stats,
day-by-day `Snapshot`/`Decision` pairs across a warm-up + served window — and serialize
it to the JSON artifact `U2` loads.

**Depends on:** none — this is the foundation everything else builds on.

**Files:** `backend/precompute.py`, `backend/artifact.py`,
`docs/decisions/0002-generated-json-artifact-over-database.md`, `tests/test_precompute.py`

**Key points (see the plan's U1 section and Key Technical Decisions for full detail):**
- Debt ledger accrues interest daily (`apr/365`, matching `engine/interest.py`) before
  applying payments/sweeps.
- Tracks a rolling 7-day `swept_this_week` so `WEEKLY_CAP` can actually bind.
- Derives `CashEvent`s from the household spec at full confidence, with a small synthetic
  `amount_low`/`amount_high` spread and `date_jitter_days` — not a degenerate point
  estimate — reusing `sim/household.py`'s private cadence helpers.
- Asserts the served window contains at least one `SWEEP` day and one `REFUSE` day;
  fails the build otherwise.
- Committed artifact (not generated at deploy time) — see U8b.

**Verification:** Running the precompute script against the committed demo spec/seed
produces a valid artifact containing ≥1 sweep and ≥1 refuse, and `tests/test_precompute.py`
passes.

Full detail: `docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md` § U1.
