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

> **Corrected 2026-07-22 (measured, before building).** This ticket opened by claiming "nothing in
> the rig is designed to produce a sweep", citing the linked demo's `card_behavior_unknown`. Running
> the existing `SHAPES × SEEDS` population through `replay()`'s wiring — 1,350 graded days — gives
> **10.6% `SWEEP`** (143 days), 59.7% `CADENCE_HOLD`, 20.0% `CARD_BEHAVIOR_UNKNOWN`, 9.7%
> `NO_SURPLUS`, against a cadence ceiling of 14.3%. The rig sweeps. The linked demo household refuses
> for a *data* reason — under three observed cycles — which is a different problem with a much
> shorter fix. **Try re-seeding the demo from an existing archetype first**; if that clears the
> opening frame, this ticket is off the demo's critical path and stands on its own merits as a
> proof-rig investment.

What is actually missing is events. The rig measures the engine across statistical shapes and never
across the things that happen *to* a household, so no case exists that is designed to exercise a
specific event and assert a specific class of response.

## What a scenario is

A named, parameterized household layered on the existing generator, carrying a **stated expectation
about the class of decision it should produce**. The expectation is the point: "it did not crash" is
not a proof, and "this household should sweep on at least 60% of graded days" is.

    steady_sweeper            — sweeps on most days it is ALLOWED to sweep. The demo's opening
                                frame. Never state this against *all* graded days: cadence
                                (`min_days_between_sweeps=7`) caps the achievable rate at 14.3%.
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
- [ ] `steady_sweeper` sweeps on a majority of **cadence-eligible** days — days not held by
      `CADENCE_HOLD` — verified, not assumed. *(Was "a majority of graded days", which is
      unsatisfiable: cadence caps the rate at 14.3%, so the only way to pass it was to change the
      policy, which measures a different product than the one that ships.)*
- [ ] `self_inflicted_overdraft` records `overdrafted` true and `sweep_caused_overdraft` false.
- [ ] `thin_history` refuses with `CARD_BEHAVIOR_UNKNOWN` early and **stops** refusing by cycle 3.
- [ ] `calibrate.measure()` runs across scenarios, not only `SpendSpec` shapes.
- [ ] Determinism test: the same `(scenario, seed)` yields an identical history twice.

## Notes

- Spawned from the demo question "how do I prove this works without real customer data?" — the
  answer is largely built (`replay.py` + `outcome.py` + `calibrate.py`); this is one of the three
  things missing from it.
- Re-seeding the public demo household so the site stops opening on a refusal (`0057`) is **not
  blocked on this ticket** — see the correction above. Do it from an existing archetype first, and
  move it to `steady_sweeper` later if the designed scenario tells a better story.
