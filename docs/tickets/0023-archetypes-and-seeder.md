---
id: "0023"
title: The archetypes, and the seeder
type: feature
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0019", "0021", "0022", "0027", "0028"]
created: 2026-07-16
---

# The archetypes, and the seeder

Implements **U5** of the plan. Single owner: `backend-python-agent`.

**This is the ticket the whole plan exists for.** Read the plan's "The absence the seed data fills."

**Depends on:** `0019` (the unified walk — the seeder is its third consumer, **not a fourth copy**),
`0021` (the repository), `0022` (the snapshot store). **All three are done (#44).**

## ✅ UNBLOCKED — two defects found by trying to build this, neither of them in this ticket, both now fixed (#46)

**1. ~~The walk cannot simulate a multi-card household.~~ Fixed in `0027`.**

`walk()` builds one `DebtLedger` from `spec.card`, and `assemble_snapshot` passes that single
`ledger_balance` to `derive_card` for **every** card. Demonstrated: a household with a $14,000
card and a $3,000 card reports `statement_balance=14009.20` for **both**. `_select_target` would
rank them equal and pick on APR alone; the portfolio reserve would count $14,009 twice.

> `HouseholdSpec.card`'s own docstring: *"anything that reserves, ranks or forecasts must iterate
> `cards`, because reading `.card` on a two-card household is exactly the bug this feature exists
> to fix."* The walk reads `.card`.

The engine's multi-card types, reserve and ranking (`0010`–`0017`) are built and tested — but the
**walk** has only ever driven single-card households, so nothing exercised them. Needs a ledger
per card, `assemble_snapshot` taking per-card balances, and a decision about `DayRecord`'s
singular `debt_apr`/`debt_id`. **A single-card household must stay byte-identical — that is the
regression test.** Its own ticket, before this one.

**2. ~~`CardSpec.apr` is `Decimal`. Archetype D is inexpressible.~~ Settled in `0028`.**

And the fix is probably *not* a nullable APR. `decision-engine.md` §6.3 says Plaid does not
**report** APR for many issuers — the card **has** a rate; we cannot **see** it. `sim/` models the
world and the derivation models what we observe, so a card with no interest rate is false about
the world *and* would break `DebtLedger`, which needs an APR to accrue. Likely a visibility flag
(`apr_reported: bool = True`) that `derive_card` honours by passing `apr=None`. **Needs a call.**

**Both are resolved. What changed for this ticket:**

- **Archetypes B and C are buildable.** `walk()` carries a portfolio; each card has its own
  ledger, its own APR, and its own close day.
- **Archetype D changed shape, and improved.** It is no longer an `APR_UNKNOWN` refusal — with
  `0028` the engine estimates at 23%, **sweeps normally, and reports no interest saved**, because
  `interest.py` will not price a guessed rate. Assert the *absence* of `INTEREST_AVOIDED` on a
  sweep, not the presence of `APR_UNKNOWN`. Use `CardSpec(apr_reported=False)`.
- `APR_UNKNOWN` still fires for `apr is None` — a card we decline to even estimate. Seed that
  too if a refusal is wanted on the dashboard.

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
| D | ~~`apr_unknown`~~ **`apr_unreported`** | biweekly | 2, both `apr_reported=False` | the estimate: ranks on 23%, sweeps, and claims **nothing** |

> **D's row above was written before `0028` and is wrong twice over.** `APR_UNKNOWN` fires only on
> `apr is None`; `CardSpec.apr` is `Decimal`, and `derive_card` reads
> `apr=card.apr if card.apr_reported else ESTIMATED_APR` — it never emits `None`. **`APR_UNKNOWN` is
> unreachable from any `HouseholdSpec`**, so "both `apr=None`" is not constructible and the AC below
> that asked for the reason code could not be met. `0028` chose a visibility flag over a nullable
> rate deliberately: the card *has* a rate, Plaid merely does not report it. The body of this ticket
> already said all of this; the table did not get the message. Built per the body, and **renamed**:
> an archetype named after a reason code it cannot produce is exactly the drift this repo keeps
> finding in itself.

**D needs two cards, and this is not a detail.** `_select_target` (`engine/decide.py:172-206`) uses
a *lone* card's missing APR without complaint — "it costs us nothing" — and only refuses when
**every** targetable card lacks one. What two estimated cards produce is the state worth seeing: a
normal sweep with **no `INTEREST_AVOIDED` reason at all**, because `engine/interest.py` returns
`None` rather than invent a number. That is a dashboard saying *"we moved your money and cannot tell
you what it saved."*

Keep A's spec **untouched**. It is the oracle for `0019`'s refactor and for this seeder both.

## The seeder

`seed()` is the **third consumer of `0019`'s generator**, alongside `build()` and `replay()`. It
writes through `0021`'s repository — the same path a live daily job would use — and stores snapshots
through `0022`'s seam. If you find yourself re-implementing the ledger walk, stop: that is the
defect `0019` exists to close.

## Acceptance criteria

- [x] Four archetypes, in `backend/archetypes.py`, each a `HouseholdSpec`. A is `DEMO_SPEC` unchanged
      — by *reference*, not a copy that could drift.
- [x] `seed()` drives `0019`'s generator and writes via `0021`'s repository. **No new walk.**
      (`build()` was never an option: it raises on a multi-card spec, and three archetypes hold one.)
- [x] **Determinism — ADR-0002's staleness property, re-pointed:** re-seeding produces identical
      rows. Same `(spec, seed)` → same bytes, asserted.
- [x] **The regression oracle:** archetype A's seeded decisions are identical to the committed
      `backend/data/decisions.json`'s, day for day, amount for amount, reason for reason.
- [x] ~~Archetype D triggers `APR_UNKNOWN`~~ — **unsatisfiable, see the note above.** Replaced with
      the assertion `0028` actually licenses: D sweeps, and **no** day carries `INTEREST_AVOIDED`.
      Absence, not a refusal.
- [x] Archetype B exercises `_select_target` ranking across ≥2 cards with known APRs — assert the
      target is the highest-APR targetable card. **Strengthened:** B's *transactor* holds the
      portfolio's highest APR (27.99%), so a naive `max(apr)` fails the test. All 5 sweeps chose
      `card_b_high` at 24.99%.
- [x] Every write validates on the way in. **This is where the ticket found `apr_source`:** the
      repository never wrote it, and the column's `server_default` made a guessed 23% land as a
      reported fact. "Fail fast" was neither cheap nor real until the column was named in the
      INSERT.

## ✅ Done. The finding is not the one this ticket predicted.

`0023` was written to price §9.3's spacing rule. **The income gate fires first, so §9.3 never gets
to run.** All four archetypes carry `payroll.variation = 0.02` — their income is, by construction,
exactly as regular as the demo's — and the engine measures B at up to **0.326** and C at **0.707**
against a **0.25** gate, refusing them 33 and 19 days out of 90.

`precompute.INCOME_BUCKET_DAYS = 28` is why, and its own comment predicted the mechanism without
noticing it generalizes: bucketing a *biweekly* earner by calendar month scores the
~4-times-a-year three-paycheck month as a 24% swing and trips a 25% gate on a household whose income
is perfectly regular. A 28-day bucket "is the honest measure of a biweekly earner's variability."
**Of a biweekly earner's.** It divides evenly into that calendar and into no other. The fix for the
demo household is the bug for everyone else, and it came within one point of being visible in the
case it was written for.

Not fixed here. `INCOME_BUCKET_DAYS` and §9.3 are the same defect wearing two hats — a
biweekly-shaped constant applied to everyone — and both wait on the recurring-income detector §6.2
assumes away. Pinned in `tests/test_seed.py::TestTheIncomeGateIsBiweeklyShaped`.

## ⚠️ Run `calibrate` across the new archetypes. Report. Do not re-baseline.

[`prd.md`](../prd.md) §5.2 and §5.3's headline numbers — **2.3% breach, 0 in 590 sweeps, ~$544K
false-refusal cost** — were measured on the old population and are cited in `prd.md`,
`strategy.md`, and `decision-engine.md`.

- [x] `python -m backend.calibrate` runs across the new archetypes and the numbers are **reported in
      the PR**. Added as `measure_archetypes()` — a *second* population printed beside the dial
      sweep, never folded into it.
- [x] They did not move. `measure(None)` is **unchanged at 4,320 days / 2.338% / 0 in 590 /
      $544,640.58**, and no dial setting is licensed, exactly as before. The archetypes are 80
      further households (4 × 20 seeds) reported separately, because the spend population measures
      one spend model across three shapes on **one calendar**, and this measures one spend model
      across **four calendars**. An average of the two answers neither question.

**Measured, at the shipped dial:**

| archetype | graded | breach% | sweep-caused overdrafts | false-refusal cost |
|---|---|---|---|---|
| `demo_biweekly` | 1440 | 1.2% | 0 | $104,825.11 |
| `semimonthly_portfolio` | 664 | **19.7%** | 0 | $83,391.00 |
| `monthly_thin` | 1060 | **7.5%** | 0 | $324,465.76 |
| `apr_unreported` | 1440 | 0.1% | 0 | $41,378.17 |
| all | 4604 | 5.0% | **0** | $554,060.04 |

**Read `graded` before `breach%`.** Every archetype is offered 1,800 days; a blocking refusal never
ran a forecast and is not graded. B is **unserved 63% of the time**, C 41%. Then on the days they
*are* served the forecast is far worse — and B's **19.7%** is the same magnitude as the **19.8%**
this very harness **refused** to ship as the empirical spend model. We declined to give everyone a
forecast as bad as the one semimonthly earners already get.

**The guardrail holds everywhere: 0 sweep-caused overdrafts across all four.** §5.2 does not move.
This is a service-and-honesty cost, not a safety one — the buffer and the obligation reserve absorb
a badly calibrated forecast, which is what they are for. **Nothing here licenses touching a gate.**

**If B/C/D refuse constantly, that is a finding, not a bug.** It is §9.3's cost made visible, and it
is why these archetypes exist. **Do not "fix" it by loosening a gate** — [`prd.md`](../prd.md) §2.2's
variance gate and the spacing rule are not dials to reach a prettier dashboard.

## Out of scope

The cash-cycle spacing rule §9.3 actually wants. These archetypes **price** it; pricing is not
fixing, and the fix needs the recurring-income detector §6.2 lists as assumed away.
