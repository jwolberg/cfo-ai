---
id: 3
title: "engine/outcome.py — grade a decision against what actually happened"
status: open
priority: high
horizon: now
hitl: false
type: feature
source: manual
created: 2026-07-13
updated: 2026-07-13
prs: []
refs: ["docs/prd.md#5.2", "docs/prd.md#5.3", "docs/strategy.md#3"]
depends_on: [1]
agent_id: 1000x-ai-engineer
agent_scope: global
agent_kind: classic
---

## Description

`decide()` emits a prediction and nothing in the repo ever settles the bet. There is no
type that compares a `Decision` to the realized future, which means the engine's central
claim — *"this $220 is not needed"* — has never been graded.

[`strategy.md`](../../docs/strategy.md) §3 says the only asset that compounds is
**calibration**: "the empirical distribution of our own errors." This ticket is that
distribution's data structure. Without it, every proposed forecast improvement loosens the
forecast and buys a bigger sweep with no evidence — which is exactly the failure
[`decision-engine.md`](../../docs/decision-engine.md) §3 forbids, turned on ourselves.

## Acceptance criteria

- `engine/outcome.py` exposes a pure `grade(snapshot, decision, realized) -> Outcome`,
  where `realized` is the funding account's actual daily balances over the horizon.
- `Outcome` is frozen and carries at least:
  - `overdrafted: bool` — realized low went below zero. This is
    [`prd.md`](../../docs/prd.md) §5.2's guardrail metric, the hard gate that outranks the
    primary KPI.
  - `breached_buffer: bool` — realized low went below the user's `buffer_floor` (a near
    miss; the buffer did its job but we spent it).
  - `projection_error: Decimal` — `realized_low − projected_low`, **signed**. Negative
    means we were too optimistic. This one field is the calibration asset.
  - `false_refusal_cost: Decimal` — on a refusal, how much *was* in fact safe to sweep in
    hindsight (`realized_low − buffer − reserved`, floored at zero). §5.3 names this "our
    cost of conservatism."
  - `interest_avoided: Decimal | None` — from #2, when a sweep happened and APR is known.
- Grading is **counterfactual-aware**: on a SWEEP, the realized balances passed in must
  already reflect the swept money leaving. The signature must make it impossible to grade a
  sweep against an untouched ledger (see #4 — this is the bug that makes a shadow report a
  lie).
- A refusal is graded too. Refusals are decisions; an ungraded refusal is how a system
  quietly under-sweeps forever ([`decision-engine.md`](../../docs/decision-engine.md) §3.1).
- Tests against #1's generated households: a household with a realized overdraft is graded
  `overdrafted`; a household where the engine refused but the realized low stayed far above
  the buffer produces a large `false_refusal_cost`; `projection_error` is exactly zero when
  the realized path matches the projection.

## Design notes

Keep `Outcome` a pure value type with no aggregation logic — the fleet-level statistics
(breach rate, error quantiles, the frontier) belong to #4 and to whatever reporting sits
above it. One decision, one grade.

The sign convention on `projection_error` will be load-bearing for years. Pick it, state it
in the docstring, and make a test assert the direction, because a flipped sign here would
silently invert the calibration dial.
