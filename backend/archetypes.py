"""The households this engine has never seen.

Every household it has ever run against is **biweekly, one card, 23.99% APR** — including all
sixty in the calibration population, because `calibrate._spec_for()` varies only the spend shape.
Sixty households deep on one axis is one household on every other axis.

So `decision-engine.md` §9.3's admission — *"a fixed 7-day spacing is a decent approximation of a
biweekly household and a poor one for everyone else"* — has never been tested, because there has
been no everyone else. These four are the everyone else.

**They price a mismatch; they do not fix it.** If B and C refuse constantly that is the finding,
not a bug, and the gates are not dials to reach a prettier dashboard (`prd.md` §2.2). The rule
`0017` established holds here: the reserve work *tightens* and is always safe; loosening needs a
measurement first.

## One axis at a time

**Annual income is held constant at $67,600 across all four** — $2,600 × 26 biweekly, $2,816.67 ×
24 semimonthly, $5,633.33 × 12 monthly. Bills and the spend shape are `DEMO_SPEC`'s throughout.
That is deliberate and it is the same discipline `calibrate._spec_for()` uses when it forces
`card_share` constant across spend shapes: if the archetypes differed in *wealth* as well as
*cadence*, nothing they measured could be attributed to either. What varies is the calendar and
the portfolio. Nothing else.

## The instrument these are measured with used to be biweekly-shaped — and these households found it

`income_variation` once summed income into three trailing 28-day buckets. That is exactly two
biweekly pay periods, so it read a biweekly earner (A) as regular (~0%) but **aliased against every
other cadence**: a semimonthly (B, 24/yr) or monthly (C, 12/yr) earner's paydays drift against the
28-day grid, so a bucket periodically caught one or three checks — or zero — and the gate (25%)
refused `INCOME_TOO_VARIABLE` on a household whose income is, by construction, exactly as regular as
A's. This module said the artifact was "something the seeder and `calibrate` are here to find out",
and they did: measured, B tripped the gate 37% of days and C 21%, A never.

**The fix (ticket `0063`) measures paycheck *amounts*, not time-bucketed sums** — timing-agnostic,
so all three cadences read the same ~1-2%, and the archetype breach rate fell on every shape with
zero sweep-caused overdrafts. These four archetypes are why a single-calendar spend population was
never enough: the defect lived in the *calendars*, and only B and C could surface it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from backend.precompute import DEMO_SPEC
from engine.models import PaymentBehavior, money
from sim.household import CardSpec, HouseholdSpec, PayCadence, PayrollSpec

# $67,600/yr, the demo persona's income, expressed in each calendar. Held constant so that any
# difference these archetypes surface is attributable to the cadence and not to the money.
BIWEEKLY_NET = money("2600.00")  # x26 — DEMO_SPEC's own
SEMIMONTHLY_NET = money("2816.67")  # x24
MONTHLY_NET = money("5633.33")  # x12


# --- A — the regression anchor --------------------------------------------------------
#
# `DEMO_SPEC` itself, by reference and not by copy. It is the oracle for `0019`'s refactor and for
# this seeder both: archetype A's seeded decisions must be identical to the committed
# `backend/data/decisions.json`, day for day and reason for reason. A copy that drifted by one
# field would break that silently, and the whole value of an oracle is that it cannot.
demo_biweekly = DEMO_SPEC


# --- B — a portfolio, on a semimonthly calendar ---------------------------------------
#
# Three cards, mixed rates and mixed behaviour, paid on the 15th and the last day of the month.
#
# **The transactor holds the highest APR on purpose.** `_select_target` (`engine/decide.py:172`)
# ranks on APR *among the cards it is honest to sweep to at all*, and a transactor is never one of
# them: they clear the statement every month, so the grace period already does what our sweep
# claims to do, and moving their cash there is a prepayment we would be charging for. A naive
# "highest APR wins" picks `card_b_transactor` at 27.99%. The correct answer is `card_b_high` at
# 24.99%. That gap is the assertion, and it is why B's cards are shaped this way rather than
# merely being three of them.
#
# The close days differ — 20th, 8th, 26th — because real portfolios do, and because `0027` fixed
# `DebtLedger` to post on *each card's* close day rather than a module constant. A portfolio whose
# cards all closed on the same day would leave that fix unexercised, which is the exact failure
# this whole ticket exists to stop repeating.
semimonthly_portfolio = HouseholdSpec(
    opening_balance=money("3200.00"),
    payroll=PayrollSpec(
        net_pay=SEMIMONTHLY_NET,
        cadence=PayCadence.SEMIMONTHLY,
        first_payday=date(2026, 1, 15),
        variation=Decimal("0.02"),
    ),
    bills=DEMO_SPEC.bills,
    spend=DEMO_SPEC.spend,
    cards=(
        CardSpec(
            balance=money("9000.00"),
            apr=Decimal("0.2499"),
            minimum_payment=money("180.00"),
            payment=money("300.00"),
            payment_day_of_month=20,
            card_id="card_b_high",
            behavior=PaymentBehavior.REVOLVER,
            close_day_of_month=20,
        ),
        CardSpec(
            balance=money("4500.00"),
            apr=Decimal("0.1799"),
            minimum_payment=money("90.00"),
            payment=money("150.00"),
            payment_day_of_month=8,
            card_id="card_b_low",
            behavior=PaymentBehavior.REVOLVER,
            close_day_of_month=8,
        ),
        CardSpec(
            # Cleared every month, so it accrues nothing and is not somewhere we can honestly
            # send money — but it is still fully reserved against (`untouchable()`), and it takes
            # the largest share of card spend. That is the shape `sim/household.py`'s own docstring
            # names as the reason `cards` is a tuple: sweep optimally to the 24% card while this
            # one quietly takes its charges and comes due.
            balance=money("1200.00"),
            apr=Decimal("0.2799"),
            minimum_payment=money("35.00"),
            payment=money("1200.00"),
            payment_day_of_month=26,
            card_id="card_b_transactor",
            behavior=PaymentBehavior.TRANSACTOR,
            close_day_of_month=26,
            charge_weight=3.0,
        ),
    ),
)


# --- C — §9.3 at its worst ------------------------------------------------------------
#
# Paid once a month, on the last day. `decision-engine.md` §9.3's fixed 7-day sweep spacing gives
# this household ~4.3 sweep windows a month against **one** payday: surplus appears when they are
# paid, and the rule decides on a calendar that has nothing to do with theirs. If any archetype
# prices §9.3, it is this one.
#
# The opening balance is a month of expenses rather than DEMO_SPEC's $3,200, and that is not
# generosity — a monthly earner who did not hold one would be insolvent by the 3rd of every month,
# which would make this a household about opening balances instead of a household about cadence.
monthly_thin = HouseholdSpec(
    opening_balance=money("6000.00"),
    payroll=PayrollSpec(
        net_pay=MONTHLY_NET,
        cadence=PayCadence.MONTHLY,
        first_payday=date(2026, 1, 31),
        variation=Decimal("0.02"),
    ),
    bills=DEMO_SPEC.bills,
    spend=DEMO_SPEC.spend,
    cards=(
        CardSpec(
            balance=money("11000.00"),
            apr=Decimal("0.2299"),
            minimum_payment=money("220.00"),
            payment=money("360.00"),
            payment_day_of_month=20,
            card_id="card_c_high",
            behavior=PaymentBehavior.REVOLVER,
            close_day_of_month=20,
        ),
        CardSpec(
            balance=money("3000.00"),
            apr=Decimal("0.1999"),
            minimum_payment=money("60.00"),
            payment=money("100.00"),
            payment_day_of_month=12,
            card_id="card_c_low",
            behavior=PaymentBehavior.REVOLVER,
            close_day_of_month=12,
        ),
    ),
)


# --- D — the rate nobody will tell us -------------------------------------------------
#
# **Named `apr_unreported`, not `0023`'s `apr_unknown`, because `APR_UNKNOWN` cannot fire here —
# and could not fire from any `HouseholdSpec` at all.**
#
# The ticket's table predates `0028` and asks for "2 cards, both `apr=None`". That card is not
# constructible: `CardSpec.apr` is `Decimal`, and `0028` settled deliberately on a visibility flag
# rather than a nullable rate, because the card *has* a rate — the household is charged it daily
# and `DebtLedger` needs a number to accrue at. `sim/` models the world; the derivation models what
# we can see. So `derive_card` (`backend/precompute.py:698`) reads
# `apr=card.apr if card.apr_reported else ESTIMATED_APR` and **never** emits `None`, while
# `APR_UNKNOWN` fires only on `apr is None`. Naming this archetype after a reason code it is
# structurally incapable of producing would be a lie of exactly the kind this repo keeps finding.
#
# What it produces instead is better, and `0023`'s own body says so: both rates are estimated at
# 23%, both cards are targetable, the engine **ranks on the guess and sweeps normally**, and
# `interest.py` refuses to price an `ESTIMATED` rate — so the household gets its money moved and
# the dashboard says **nothing** about what it saved. *Act on the estimate; never bill for it.*
#
# The two real rates differ (26.99% and 18.99%) and the engine sees 23% for both. It will
# therefore sometimes rank them wrongly — which is precisely the trade `0028` argued is safe:
# `APR_UNKNOWN` is not a safety gate, a wrong target optimizes worse and overdraws nobody.
apr_unreported = HouseholdSpec(
    opening_balance=money("3200.00"),
    payroll=PayrollSpec(
        net_pay=BIWEEKLY_NET,
        cadence=PayCadence.BIWEEKLY,
        first_payday=date(2026, 1, 2),
        variation=Decimal("0.02"),
    ),
    bills=DEMO_SPEC.bills,
    spend=DEMO_SPEC.spend,
    cards=(
        CardSpec(
            balance=money("7000.00"),
            apr=Decimal("0.2699"),
            minimum_payment=money("140.00"),
            payment=money("230.00"),
            payment_day_of_month=20,
            card_id="card_d_hidden_high",
            behavior=PaymentBehavior.REVOLVER,
            close_day_of_month=20,
            apr_reported=False,
        ),
        CardSpec(
            balance=money("5000.00"),
            apr=Decimal("0.1899"),
            minimum_payment=money("100.00"),
            payment=money("165.00"),
            payment_day_of_month=14,
            card_id="card_d_hidden_low",
            behavior=PaymentBehavior.REVOLVER,
            close_day_of_month=14,
            apr_reported=False,
        ),
    ),
)


# The name is written to `households.archetype`, whose column comment says why it is nullable:
# "Null for a real household, which is the point of recording it: synthetic and real must be
# tellable apart in any number either one appears in."
ARCHETYPES: dict[str, HouseholdSpec] = {
    "demo_biweekly": demo_biweekly,
    "semimonthly_portfolio": semimonthly_portfolio,
    "monthly_thin": monthly_thin,
    "apr_unreported": apr_unreported,
}
