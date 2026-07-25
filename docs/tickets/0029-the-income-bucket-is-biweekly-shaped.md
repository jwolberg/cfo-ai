---
id: "0029"
closed: 2026-07-22
title: The income bucket is biweekly-shaped — the gate refuses households whose income is regular
type: bug
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0023-archetypes-and-seeder.md
depends_on: ["0023"]
created: 2026-07-16
---

# The income bucket is biweekly-shaped — the gate refuses households whose income is regular

> **CLOSED 2026-07-22 — resolved by [`0063`](0063-income-variation-aliases-against-pay-cadence.md),
> not under this number.** This ticket and `0063` describe the same defect (the 28-day income bucket
> aliases against non-biweekly pay cadences). `0063` measured it with `calibrate`, replaced the
> bucketed metric with the coefficient of variation of paycheck *amounts*, and flipped the tripwire
> `TestTheIncomeGateIsBiweeklyShaped` → `TestTheIncomeGateIsCadenceAgnostic`. Verified done
> 2026-07-23: that test exists (`tests/test_seed.py:350`) and the old biweekly-shaped one is gone.
> Kept as a record; do not rebuild.

**Found by `0023`, which was looking for something else.** Not in its ticket, and not fixed there:
it has a measured cost, no safe local fix, and it **loosens** — which is the one direction
`decision-engine.md` §3 forbids without evidence.

**Files:** `backend/precompute.py` (`income_variation`, `INCOME_BUCKET_DAYS`), `engine/decide.py`
(`MAX_INCOME_VARIATION`), `docs/decision-engine.md` §6.2/§9.3, `docs/prd.md` §2.2, tests.

## The defect

`income_variation()` (`backend/precompute.py:445`) is the coefficient of variation of payroll across
**three trailing 28-day buckets**. `engine/decide.py` refuses above `MAX_INCOME_VARIATION = 0.25`
with `INCOME_TOO_VARIABLE`.

A 28-day bucket divides evenly into a biweekly calendar — exactly two paychecks, every bucket,
forever — and into **no other calendar**. A semimonthly earner (24/yr, ~15.2-day gaps) lands 1 or 2
paychecks per bucket. A monthly earner (12/yr, ~30.4-day gaps) lands 0 or 1. The buckets alternate,
the CV explodes, and the gate refuses a household whose income never varied.

Measured across `0023`'s archetypes, **all of which carry `payroll.variation = 0.02`** — their
income is, by construction, exactly as regular as the demo household's:

| archetype | true variation | measured `income_variation` | days over the 0.25 gate |
|---|---|---|---|
| A biweekly | 0.02 | 0.003–0.012 | 0/90 |
| B semimonthly | 0.02 | 0.004–**0.326** | **33/90** |
| C monthly | 0.02 | 0.007–**0.707** | **19/90** |
| D biweekly | 0.02 | 0.003–0.012 | 0/90 |

## Why this is worth reading rather than just fixing

**The constant's own comment predicted the mechanism and missed that it generalizes.**
`INCOME_BUCKET_DAYS` exists *because* someone found this bug already — in the biweekly case:

> *"This household is paid biweekly, so a calendar month contains two paychecks — except the ~4
> times a year it contains three. Bucketing by month would score that calendar artifact as a 24%
> swing in income and trip `INCOME_TOO_VARIABLE` (the gate is 25%), refusing to serve a household
> whose income is in fact perfectly regular. A 28-day bucket is the honest measure of a biweekly
> earner's variability."*

Every word is true. **The fix for the demo household is the bug for every other household**, and it
came within *one percentage point* — a 24% artifact against a 25% gate — of being visible in the
very case it was written for. It survived because there was no everyone else: `DEMO_SPEC` is
biweekly, and so are all 60 households in the calibration population, because
`calibrate._spec_for()` varies only the spend shape.

`prd.md` §2.2's variance gate is working correctly. It is working on a number that is wrong.

## This is a loosening, and it admits households onto a forecast we have measured as bad for them

**Do not fix this in isolation, and do not treat it as a bug fix.** It changes *who we serve*.

`0023` measured what happens on the days B and C **are** served today:

| archetype | graded | breach% | sweep-caused overdrafts |
|---|---|---|---|
| `demo_biweekly` | 1440 | 1.2% | 0 |
| `semimonthly_portfolio` | 664 | **19.7%** | 0 |
| `monthly_thin` | 1060 | **7.5%** | 0 |

B is graded on 664 of 1,800 offered days because the income gate blocks the rest — a blocking
refusal never runs a forecast. So **the broken gate is currently the thing standing between a
semimonthly household and a forecast that breaches 19.7% of the time**, which is the same magnitude
as the empirical spend model this harness **refused** to ship (19.8% against today's 2.3%).

Fixing the gate hands those ~33 days a month back to that forecast. The newly admitted days are the
ones nearest a pay-cycle boundary, which is where the projection has least reason to be right — so
assuming the breach rate merely stays at 19.7% is optimistic, and it is not a number anyone has.

**The guardrail is 0 sweep-caused overdrafts across all four archetypes today. That is the thing
this ticket could break**, and `prd.md` §5.2 says it outranks everything else here.

## The fix nobody can write yet

The honest bucket is the household's **own pay cycle** — decide once per cycle, measure variation
across cycles. For a semimonthly earner that is two buckets a month; for a monthly earner, one. That
needs the **recurring-income detector** `decision-engine.md` §6.2 lists as assumed away.

**This is the same dependency, and the same defect, as §9.3.** §9.3's fixed 7-day sweep spacing is
"a decent approximation of a biweekly household and a poor one for everyone else"; the 28-day income
bucket is a decent measure of a biweekly household and a poor one for everyone else. Two constants,
one shape: *a biweekly-shaped instrument applied to everyone*, both waiting on the same unbuilt
detector. They should probably be one piece of work.

**What is explicitly not the fix:** raising `MAX_INCOME_VARIATION` past 0.33 (or 0.71). That admits
B and C by admitting *genuinely* variable households too — which is precisely what §2.2 exists to
refuse, and the literature behind it (Telyukova 2013; Fulford 2015) says their buffer is the correct
hedge and sweeping it is worse advice than doing nothing. The gate is not a dial to reach a prettier
dashboard.

## Acceptance criteria

- [ ] Income variability is measured over the household's own pay cycle, not a fixed 28-day window.
      A biweekly household's measurement is **unchanged** — `backend/data/decisions.json` stays
      byte-identical, and that is the regression test.
- [ ] `income_variation` for B and C falls to approximately their true `payroll.variation` (0.02),
      because that is what it is.
- [ ] A **genuinely** variable household still trips `INCOME_TOO_VARIABLE`. Add one to
      `backend/archetypes.py` — there isn't one, which is its own gap: nothing has ever tested that
      the gate fires when it *should*, only that it fires.
- [ ] `python -m backend.calibrate` re-run across the archetypes, and **reported as a change**.
      `measure_archetypes()` exists for exactly this (`0023`).
- [ ] **Hard gate: 0 sweep-caused overdrafts across all four archetypes, or it does not ship.**
      `prd.md` §5.2. Serving more days must not buy an overdraft.
- [ ] If the breach rate on the newly served days is bad — and 19.7% says it may be — **that is the
      finding, and the answer is not to ship it.** The gate stays broken and the households stay
      refused until the forecast can carry them, which is a worse outcome that is honestly come by.
      Say so in `decision-engine.md` §9.3 rather than quietly leaving the ticket open.

## Pinned

`tests/test_seed.py::TestTheIncomeGateIsBiweeklyShaped` asserts the defect: all four archetypes have
identical true income regularity, A and D are never refused for variance, B and C are. It fails the
day this is fixed — deliberately. That is the test telling you to come back and update it with a
measurement, not a test that has become wrong.
