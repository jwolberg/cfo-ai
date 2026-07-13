---
id: 2
title: "engine/interest.py — counterfactual amortization and interest avoided"
status: in-progress
priority: high
horizon: now
hitl: false
type: feature
source: manual
created: 2026-07-13
updated: 2026-07-13
prs: []
refs: ["docs/prd.md#5.1", "docs/prd.md#1", "docs/decision-engine.md#6.3"]
depends_on: []
agent_id: 1000x-ai-engineer
agent_scope: global
agent_kind: classic
---

## Description

The primary KPI ([`prd.md`](../../docs/prd.md) §5.1) is **realized interest avoided,
measured against the user's own pre-signup payment trajectory** — and that trajectory is
an amortization model the repo does not have. The headline product line in §1 (*"that's
$31 of interest you won't pay"*) is a financial claim already written into the product
copy that `explain.py` cannot currently compute.

Add the smallest honest interest model: given a card's balance, APR, and minimum-payment
rule, project the payoff path. The difference between the no-sweep path and the with-sweep
path is both the KPI and the sentence.

## Acceptance criteria

- `engine/interest.py` exposes a pure function projecting a card's balance forward under a
  stated payment schedule, returning the total interest paid over the payoff (or over a
  bounded horizon, with the bound explicit).
- `interest_avoided(debt, sweep_amount, schedule) -> Decimal` returns the delta between
  the counterfactual path (minimums only) and the path with the sweep applied.
- All money is `Decimal` through `money()`. Daily-periodic-rate rounding is applied once,
  at a stated convention, and the convention is documented in the module docstring — a
  cent of drift per day compounds into a wrong customer-facing claim.
- **APR is `None` for many issuers** ([`decision-engine.md`](../../docs/decision-engine.md)
  §6.3). The function must return `None` rather than guess a rate. `explain.py` renders a
  sweep with no dollar figure attached in that case, and there is a test asserting we never
  emit an invented interest number.
- A `Decision` carrying a sweep can be rendered with its interest claim; a new `ReasonCode`
  or an optional field on `Decision` carries it (design decision is the ticket's to make —
  record it in `docs/implementation-notes.md`).
- Tests: a known balance/APR/minimum reproduces a hand-computed amortization schedule to
  the cent; a $0 sweep avoids $0 of interest; a sweep that clears the card avoids exactly
  the remaining interest.

## Design notes

Resist scope creep into statement cycles, grace periods, and cash-advance vs purchase APR
buckets. A revolver carrying a balance has no grace period and interest accrues daily on
the average daily balance — that single case is the one the target user is in ([`prd.md`](../../docs/prd.md)
§3) and it is enough. Note the simplification in `docs/implementation-notes.md` rather than
building the general case.

The rounding convention matters more than it looks: this number appears in the customer's
notification *and* in the KPI the company is graded on, and those two must never disagree.
