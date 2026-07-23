---
id: "0063"
title: Income variation aliased against pay cadence — measure paycheck amounts, not time buckets
type: fix
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
created: 2026-07-22
---

# Income variation aliased against pay cadence

Spawned from operating the console (2026-07-22): a monthly earner's "income variation" jumped to
**70.71%** on certain days and refused the household `INCOME_TOO_VARIABLE`, even though its pay is —
by construction — perfectly regular. The archetypes doc had predicted exactly this and left it as
"something the seeder and `calibrate` are here to find out." They found it.

## The defect

`precompute.income_variation` summed payroll into **three trailing 28-day buckets** and took the
coefficient of variation of the bucket sums. 28 days is exactly two biweekly pay periods and 84 is
exactly six, so a biweekly earner always landed two checks per bucket → ~0%. But 84 is not a clean
multiple of a semimonthly (~15.2-day) or monthly (~30.4-day) period, so their paydays drift against
the bucket grid, and a bucket periodically caught one or three checks — or, at the worst alignment,
**zero** (the `[X, X, 0]` triple → √2⁄2 = 70.71%). The gate is 25%, so the metric refused regular
earners. Measured across the archetype population (4 cadences × 20 seeds):

| cadence | days over the 25% gate |
|---|---|
| biweekly | 0% |
| semimonthly | **37%** |
| monthly | **21%** |

`income_too_variable` accounted for **14.4%** of all archetype decisions — every one of them
semimonthly or monthly, none biweekly.

## The fix

Measure the coefficient of variation of individual **paycheck amounts** over the trailing ~84 days,
never summing into a time grid. It is timing-agnostic, so it asks the question the gate actually
cares about — *do the paychecks vary in size?* — and reads ~1–2% for a regular earner at any
cadence. A missed payday is a *gap*, not amount-volatility, and the forecast already catches it (an
expected inflow that does not arrive lowers the projected low).

## Measured impact (calibrate, archetype population, before → after)

- **Sweep-caused overdrafts: 0 → 0** (the hard veto held).
- Overall breach rate: 4.97% → **4.15%**; worst shape (semimonthly): 19.73% → **10.95%**. Every
  shape same-or-better, so the change is licensed by `calibrate`'s own rule.
- `income_too_variable` refusals: 1,040 (14.4%) → **0**.
- Sweeps: 9.2% → 10.2% — the semimonthly and monthly households now pay down debt on the days they
  were wrongly refused (`card_b_high` reaches $0 within the window).

## Acceptance criteria

- [x] `income_variation` no longer aliases against cadence — `tests/test_seed.py` pins that no
      regular-income archetype is ever refused `income_too_variable`.
- [x] The engine change is measured, not asserted: breach rate no worse on any shape, 0 sweep-caused
      overdrafts.
- [x] The stored snapshots are re-seeded; the operator-console "income variation" copy and the
      trace label describe the paycheck metric, not the old buckets.

## Notes

- The old `TestTheIncomeGateIsBiweeklyShaped` was a deliberate tripwire ("so that fixing it is a
  deliberate act with a measurement attached"). It is now `TestTheIncomeGateIsCadenceAgnostic` and
  pins the fix.
- Left open elsewhere: `decision-engine.md` §9.3's spacing rule was *masked* by this gate firing
  first; with the mask gone it may now surface on its own and is worth a look. Not in this fix.
