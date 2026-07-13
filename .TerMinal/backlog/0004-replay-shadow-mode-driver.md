---
id: 4
title: "engine/replay.py — point-in-time shadow-mode driver"
status: open
priority: high
horizon: now
hitl: false
type: feature
source: manual
created: 2026-07-13
updated: 2026-07-13
prs: []
refs: ["docs/prd.md#8", "docs/decision-engine.md#2.1", "docs/decision-engine.md#6.1", "docs/strategy.md#3"]
depends_on: [1, 2, 3]
agent_id: 1000x-ai-engineer
agent_scope: global
agent_kind: classic
---

## Description

[`prd.md`](../../docs/prd.md) §8 puts exactly one thing in the **Now** column: run the
engine in shadow mode — compute what we *would* have swept, then check it against what
actually happened, and get a measured tail-risk number before any money moves.

This is the driver. It walks a household's history forward day by day, builds a `Snapshot`
as of each day, calls `decide()`, and grades with #3. It is the payoff for the
no-clock/frozen-`Snapshot` discipline in
[`decision-engine.md`](../../docs/decision-engine.md) §2.1 — a design choice made for
exactly this and never yet cashed in.

## Acceptance criteria

- `engine/replay.py` (or `sim/replay.py` if it must import from `sim/`) exposes
  `replay(history, policy) -> tuple[Outcome, ...]`, one graded outcome per simulated day.
- **Point-in-time correctness.** The `Snapshot` for day *T* is built only from data
  knowable on day *T* — `balance_age_days`, `history_days`, pending transactions, and
  detected events all as-of *T*. There is a test that fails if a future transaction leaks
  into a past snapshot. (Plaid restates history — pending rows are replaced with different
  ids, banks reverse and re-post — so a backtest built from the *current* ledger has
  lookahead baked in and reports a flattering, false tail risk.)
- **The sweep feedback loop is closed.** A simulated sweep removes money from the ledger
  and every subsequent day's balance and decision reflect it, including `sweeps_in_flight`
  during ACH settlement and `swept_this_week` against the weekly cap. A replay that grades
  each day against the *untouched* real history never compounds the effect of its own
  actions and will systematically understate breach risk. There is a test asserting that a
  swept dollar is absent from the next day's projected balance.
- Fleet aggregation: given many households, report **sweep-caused overdraft rate**
  (§5.2's hard gate), the `projection_error` distribution with its negative tail
  quantiles, total interest avoided, and total `false_refusal_cost`.
- **Variant comparison.** `replay` takes the engine's tunables (buffer, caps, the
  confidence/variance thresholds of
  [`decision-engine.md`](../../docs/decision-engine.md) §6.1) as parameters, so two engine
  configurations can be run over the *same* households and plotted against each other:
  interest avoided on one axis, breach rate on the other.
- Deterministic end to end — same households + same config → same outcomes, forever.

## Design notes

The variant-comparison frontier is the actual deliverable, and it is what makes three
things in the docs mechanical instead of rhetorical:

- §8's *"raise the ceiling as calibration proves out"* becomes a computation, not a
  judgment call.
- §6.1's five uncalibrated magic numbers (`INCOME_CONFIDENCE_FLOOR`,
  `MAX_INCOME_VARIATION`, `MAX_BALANCE_AGE_DAYS`, the buffer, the caps) become one dial
  with a measured meaning.
- [`strategy.md`](../../docs/strategy.md) §3's moat claim — *"moving more at the same
  tail risk is the whole product"* — becomes a testable proposition rather than an
  assertion.

Everything downstream is gated on this: the recurring-event detector (§6.2) cannot be
judged until we can measure whether its confidences are honest, the guarantee
([`prd.md`](../../docs/prd.md) §2.3) cannot be priced until we have a breach rate, and the
W2/spend-distribution forecast rework must not ship until it can be shown to sweep more at
the same measured risk.
