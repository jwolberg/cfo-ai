---
id: "0023"
title: The archetypes, and the seeder
type: feature
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0019", "0021", "0022"]
created: 2026-07-16
---

# The archetypes, and the seeder

Implements **U5** of the plan. Single owner: `backend-python-agent`.

**This is the ticket the whole plan exists for.** Read the plan's "The absence the seed data fills."

**Depends on:** `0019` (the unified walk — the seeder is its third consumer, **not a fourth copy**),
`0021` (the repository), `0022` (the snapshot store).

**Files:** `backend/archetypes.py`, `backend/seed.py`, `tests/test_seed.py`

## Why this is not decoration

Every household this engine has ever run against is **biweekly, one card, 23.99% APR** — including
all 60 in the calibration population, because `calibrate._spec_for()` (`backend/calibrate.py:82-84`)
is `replace(DEMO_SPEC, spend=charging)` and varies **only** the spend shape.

`PayCadence.WEEKLY`/`SEMIMONTHLY`/`MONTHLY` are exercised nowhere outside `sim/`'s own `_paydays`
and `tests/test_household.py:215`. Multi-card `cards=(...)` tuples appear only in tests — despite
`sim/household.py:229-231` saying the tuple exists because *"a portfolio reserve and a coverage gate
need more than one card to have anything to bite on."*

So [`decision-engine.md`](../decision-engine.md) §9.3's admission — *"a fixed 7-day spacing is a
decent approximation of a biweekly household and a poor one for everyone else"* — **has never been
tested, because there is no everyone else.**

## The four archetypes

| # | Name | Cadence | Cards | Stresses |
|---|---|---|---|---|
| A | `demo_biweekly` | biweekly | 1 @ 23.99% | **Regression anchor.** Today's `DEMO_SPEC`, byte-for-byte unchanged. |
| B | `semimonthly_portfolio` | semimonthly | 3, mixed APR + `PaymentBehavior` | `_select_target` ranking; the portfolio reserve; §9.3's mismatch (2 paydays vs ~4.3 sweep windows/mo) |
| C | `monthly_thin` | monthly | 2 | **§9.3 at its worst** — one payday vs ~4.3 sweep windows/mo |
| D | `apr_unknown` | biweekly | 2, **both** `apr=None` | `APR_UNKNOWN` (`engine/decide.py:199-204`) |

**D needs two cards, and this is not a detail.** `_select_target` (`engine/decide.py:172-206`) uses
a *lone* card's missing APR without complaint — "it costs us nothing" — and only refuses when
**every** targetable card lacks one. A single APR-less card produces a different state that is also
worth seeing: a normal sweep with **no `INTEREST_AVOIDED` reason at all**, because
`engine/interest.py:115-116` returns `None` rather than invent a number. That is a dashboard saying
*"we moved your money and cannot tell you what it saved."* Seed it as a variant if it is cheap.

Keep A's spec **untouched**. It is the oracle for `0019`'s refactor and for this seeder both.

## The seeder

`seed()` is the **third consumer of `0019`'s generator**, alongside `build()` and `replay()`. It
writes through `0021`'s repository — the same path a live daily job would use — and stores snapshots
through `0022`'s seam. If you find yourself re-implementing the ledger walk, stop: that is the
defect `0019` exists to close.

## Acceptance criteria

- [ ] Four archetypes, in `backend/archetypes.py`, each a `HouseholdSpec`. A is `DEMO_SPEC` unchanged.
- [ ] `seed()` drives `0019`'s generator and writes via `0021`'s repository. **No new walk.**
- [ ] **Determinism — ADR-0002's staleness property, re-pointed:** re-seeding produces identical
      rows. Same `(spec, seed)` → same bytes, asserted.
- [ ] **The regression oracle:** archetype A's seeded decisions are identical to the committed
      `backend/data/decisions.json`'s, day for day, amount for amount, reason for reason.
- [ ] Archetype D triggers `APR_UNKNOWN` on at least one day — assert the reason code, not just that
      it refused.
- [ ] Archetype B exercises `_select_target` ranking across ≥2 cards with known APRs — assert the
      target is the highest-APR targetable card.
- [ ] Every write validates on the way in (ADR-0004 [3.2]: the seeder is where "fail fast" is cheap
      and real, now that startup cannot validate every row).

## ⚠️ Run `calibrate` across the new archetypes. Report. Do not re-baseline.

[`prd.md`](../prd.md) §5.2 and §5.3's headline numbers — **2.3% breach, 0 in 590 sweeps, ~$544K
false-refusal cost** — were measured on the old population and are cited in `prd.md`,
`strategy.md`, and `decision-engine.md`.

- [ ] `python -m backend.calibrate` runs across the new archetypes and the numbers are **reported in
      the PR**.
- [ ] If they move, they are **stated as a change**, not silently written over. Updating three
      documents' figures without saying the population changed underneath them is how a measurement
      becomes a story.

**If B/C/D refuse constantly, that is a finding, not a bug.** It is §9.3's cost made visible, and it
is why these archetypes exist. **Do not "fix" it by loosening a gate** — [`prd.md`](../prd.md) §2.2's
variance gate and the spacing rule are not dials to reach a prettier dashboard.

## Out of scope

The cash-cycle spacing rule §9.3 actually wants. These archetypes **price** it; pricing is not
fixing, and the fix needs the recurring-income detector §6.2 lists as assumed away.
