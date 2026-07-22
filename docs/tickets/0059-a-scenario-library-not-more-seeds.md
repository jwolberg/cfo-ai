---
id: "0059"
title: A scenario library — designed events, not more seeds
type: feat
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-22-001-feat-proving-the-engine-harness-and-observability-plan.md
depends_on: []
created: 2026-07-22
---

# A scenario library — designed events, not more seeds

`sim/household.py` varies **spend distribution and seed**. It does not vary *what happens to a
household*. So the harness measures the engine across statistical shapes and never across the events
that actually decide whether a sweep engine earns its keep.

The visible symptom: the flagship linked demo answers `card_behavior_unknown` — a refusal. Nothing in
the rig is **designed to produce a sweep**, so the one thing a viewer most needs to see, the product
working, is absent by construction.

## What a scenario is

A named, parameterized household layered on the existing generator, carrying a **stated expectation
about the class of decision it should produce**. The expectation is the point: "it did not crash" is
not a proof, and "this household should sweep on at least 60% of graded days" is.

    steady_sweeper            — sweeps on most days. The no-decision-decision fix, and the demo's
                                opening frame.
    income_skips_a_cycle      — the forecast's worst case, arriving on schedule.
    surprise_annual_charge    — a tail event inside the horizon.
    card_appears_midstream    — coverage changes under the engine's feet mid-replay.
    statement_lands_late      — the obligation/APR path running on stale terms.
    self_inflicted_overdraft  — the household breaches on its own. `sweep_caused_overdraft` must stay
                                false: the guardrail measures *our* doing, not theirs.
    thin_history              — must defer, and must **stop** deferring once three cycles are
                                observed. A deferral that never resolves is a dead product.

## Constraints

- **Determinism is not negotiable.** Same `(scenario, seed)` → byte-identical `History`, through an
  explicitly-seeded `random.Random`, as `sim/household.py` already requires.
- **Point-in-time honesty survives.** Events are baked into the generated history, so `as_of(day)`
  still slices rather than regenerates. An event injected at replay time would reintroduce the
  lookahead bias the generator exists to prevent.
- Scenarios compose with the existing `SpendSpec` shapes rather than replacing them —
  `calibrate.measure()` must be able to run the dial across scenarios.

## Acceptance criteria

- [ ] Each scenario is expressed as data (an event schedule), not a bespoke generator per case.
- [ ] Each carries a machine-checkable expectation about its decision class, asserted in tests.
- [ ] `steady_sweeper` produces sweeps on a majority of graded days — verified, not assumed.
- [ ] `self_inflicted_overdraft` records `overdrafted` true and `sweep_caused_overdraft` false.
- [ ] `thin_history` refuses with `CARD_BEHAVIOR_UNKNOWN` early and **stops** refusing by cycle 3.
- [ ] `calibrate.measure()` runs across scenarios, not only `SpendSpec` shapes.
- [ ] Determinism test: the same `(scenario, seed)` yields an identical history twice.

## Notes

- Spawned from the demo question "how do I prove this works without real customer data?" — the
  answer is largely built (`replay.py` + `outcome.py` + `calibrate.py`); this is one of the three
  things missing from it.
- Once this lands, re-seed the public demo household from `steady_sweeper` so the deployed site stops
  opening on a refusal (`0057`).
