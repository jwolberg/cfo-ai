---
id: 1
title: "Synthetic household generator for the shadow-mode harness"
status: in-progress
priority: high
horizon: now
hitl: false
type: feature
source: manual
created: 2026-07-13
updated: 2026-07-13
prs: ["https://github.com/jwolberg/cfo-ai/pull/6"]
refs: ["docs/prd.md#8", "docs/decision-engine.md#6.1"]
depends_on: []
agent_id: 1000x-ai-engineer
agent_scope: global
agent_kind: classic
---

## Description

Shadow mode ([`prd.md`](../../docs/prd.md) §8) needs households with a *known* realized
future to grade decisions against. We have no consented users yet, and we cannot build or
test the grader (#3) or the replay driver (#4) without labeled ground truth.

A deterministic generator produces a household's full ledger — pay deposits, recurring
bills, daily discretionary spend, card balances — plus the realized daily balances that
actually followed. It is the ground truth the harness grades against, and it doubles as
the fixture library `tests/` currently lacks (every test today hand-builds a `Snapshot`).

This is a **test/simulation** artifact, not a product surface. It never ships in a
decision path.

## Acceptance criteria

- `sim/household.py` (or equivalent) exposes a pure `generate(spec, seed) -> History`
  producing a day-indexed ledger of transactions and the realized daily balance series
  per account.
- Deterministic: the same `(spec, seed)` yields a byte-identical `History` forever. No
  wall-clock reads, no ambient RNG — the seed is an explicit input.
- The spec covers, at minimum: pay cadence (weekly / biweekly / semimonthly / monthly),
  net pay amount and its variance, recurring bill calendar with per-payee amount
  distributions, a daily discretionary spend distribution that is **right-skewed and
  zero-inflated** (real spend is not Gaussian), starting balances, and card
  balance/APR/minimum.
- Injectable shocks, each individually toggleable: a missed paycheck, a one-off large
  expense, a step change in the spend baseline (regime change), and a bill that lands
  early.
- Emits money as `Decimal` via `engine.models.money()` — the generator must not be the
  thing that introduces a float into the system.
- A `History` can be sliced **as of** a given day, returning only what was knowable then
  (this is the seam #4 needs for point-in-time correctness).
- Tests: generated histories round-trip through `Snapshot` construction; a fixed seed
  reproduces exactly; the discretionary series has the requested mean/skew within
  tolerance.

## Design notes

Falsification target: build a household with a known daily discretionary distribution and
assert whether `30 × p90_daily` (what `forecast.py:100` assumes today) sits far above the
empirical p99 of that household's realized 30-day windows. If it does, the current spend
model is the binding constraint on every decision and the engine is refusing for
arithmetic reasons rather than safety ones. If it does not, that hypothesis is dead and
we drop it. Either result is worth having before touching the forecast.

Keep it in a `sim/` package, not `engine/` — the engine stays a pure decision function
with no simulation code reachable from it.
