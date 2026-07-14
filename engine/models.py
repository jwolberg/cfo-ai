"""Value types for the sweep decision engine.

Money is `Decimal` quantized to cents everywhere. No floats touch a dollar amount.
Dates are the *user's* local dates, always passed in explicitly — the engine never
reads a clock. See docs/decision-engine.md [2.1] for why.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")

# Regulation Z requires at least 21 days between a statement closing and its payment being
# due, on any card that charges interest. It is a floor, not a convention: a shorter grace
# would place the due date *earlier* than the law allows, which pulls an obligation out of
# the horizon we reserve for. Bad data must not shrink the reserve.
MIN_GRACE_DAYS = 21


def statement_day(year: int, month: int, day_of_month: int) -> date:
    """The statement close in a given month, clamped to the month's length.

    A card that closes on the 31st closes on the 28th in February. Left unclamped this
    raises, and a February crash in the interest model is not a failure mode worth having.

    Lives here rather than in engine/interest.py — which is where it was born and which now
    imports it — because StatementCycle needs it too, and interest.py imports *from* this
    module. One implementation, or the two would drift and a February bug would appear in
    exactly one of them.
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day_of_month, last))


def money(value: str | int | Decimal) -> Decimal:
    """Quantize to cents. The only way a Decimal amount should be constructed.

    Floats are rejected rather than converted. `Decimal(2.675)` is not 2.675 — it is the
    binary approximation, which quantizes to 2.67, not 2.68. A silently wrong cent is
    exactly the failure this type system exists to prevent, so an upstream float must
    fail loudly at the boundary instead of rounding quietly in the middle.
    """
    if isinstance(value, float):
        raise TypeError(
            f"money() refuses floats (got {value!r}) — pass a str or Decimal. "
            "Binary floats cannot represent cents exactly."
        )
    return Decimal(value).quantize(CENTS)


class ConnectionState(str, Enum):
    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    DISCONNECTED = "disconnected"


class AccountKind(str, Enum):
    CHECKING = "checking"
    SAVINGS = "savings"


class EventKind(str, Enum):
    """Whether an event is already accounted for elsewhere.

    The recurring-event detector will happily identify a credit card payment as a monthly
    obligation — it looks exactly like one. But the card portfolio already carries that
    obligation explicitly, and `decide()` reserves it out of available cash. Left
    undistinguished, the same payment is subtracted twice: once by the forecast, once by the
    reserve.

    **This docstring used to assert a safety property that was false.** It claimed skipping the
    card payment was safe because "we under-sweep, nobody is overdrawn" — which held only while
    the reserve was the *minimum* and the payment was a hardcoded constant. It is not true in
    general: a household pays more than the minimum (that is *why* they have idle cash), so
    skipping the whole payment while reserving only the minimum under-counts the difference, in
    the one direction that ends in an overdraft. `backend/precompute.py` had to route around
    this with an ORDINARY-at-full-value workaround, and knowingly ate a double-count to do it.

    What makes the skip correct now is that the reserve covers the card's **actual obligation**
    — both the statement that has closed and the one about to (`obligation_in_horizon`). The
    portfolio is the authoritative source; the forecast skips CARD_PAYMENT events and lets the
    reserve do that job alone. Remove either half and the arithmetic breaks in the unsafe
    direction.
    """

    ORDINARY = "ordinary"
    CARD_PAYMENT = "card_payment"


@dataclass(frozen=True)
class Account:
    account_id: str
    balance: Decimal
    connection: ConnectionState
    # Days since the balance was last confirmed with the institution.
    balance_age_days: int
    kind: AccountKind = AccountKind.CHECKING


@dataclass(frozen=True)
class CashEvent:
    """A predicted future inflow or outflow, against a specific account.

    `amount` is signed: positive is money in, negative is money out.

    `amount_low` / `amount_high` bound the plausible magnitude, and `date_jitter_days`
    bounds the timing. The forecast uses whichever end of each range is *worse* for the
    user — see forecast.conservative_low_balance.
    """

    label: str
    account_id: str
    expected_date: date
    amount: Decimal
    amount_low: Decimal  # smallest plausible magnitude (still signed)
    amount_high: Decimal  # largest plausible magnitude (still signed)
    date_jitter_days: int
    confidence: float  # P(this event occurs roughly as predicted), 0..1
    # CARD_PAYMENT events are excluded from the forecast — the card portfolio is the
    # authoritative source and decide() reserves it. See EventKind.
    kind: EventKind = EventKind.ORDINARY

    def __post_init__(self) -> None:
        """Refuse to exist if the bounds are incoherent.

        The forecast's safety property depends entirely on `amount_low`/`amount_high`
        being signed consistently with `amount`. Enter an outflow's high bound as a
        positive magnitude (-1800 nominal, +2200 "could be as much as") and the
        worst-case selection silently picks -1800, understating the outflow by $400 and
        authorising a sweep that overdraws the user. Upstream is a not-yet-written
        recurring-event detector; this is the boundary that stops its sign bug from
        becoming someone's overdraft.
        """
        for name in ("amount_low", "amount_high"):
            bound = getattr(self, name)
            if (bound > ZERO) != (self.amount > ZERO) and bound != ZERO:
                raise ValueError(
                    f"{name}={bound} has a different sign from amount={self.amount}; "
                    "bounds must be signed like the amount they bound"
                )

        if abs(self.amount_low) > abs(self.amount_high):
            raise ValueError(
                f"amount_low={self.amount_low} is larger in magnitude than "
                f"amount_high={self.amount_high}"
            )

        if self.date_jitter_days < 0:
            raise ValueError(
                f"date_jitter_days={self.date_jitter_days} must be >= 0; a negative "
                "jitter inverts the late-income/early-obligation rule"
            )

        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence={self.confidence} must be in [0, 1]")

    @property
    def is_inflow(self) -> bool:
        return self.amount > ZERO


@dataclass(frozen=True)
class PendingTransaction:
    """Authorized but not yet posted. Signed like CashEvent."""

    label: str
    account_id: str
    amount: Decimal


@dataclass(frozen=True)
class Debt:
    debt_id: str
    balance: Decimal
    minimum_payment: Decimal
    minimum_due_date: date
    # None when the issuer does not report it through Plaid. This is common, and the
    # engine must not pretend otherwise. See docs/decision-engine.md [4.3].
    #
    # An APR is a *rate*, not a dollar amount — do not construct it with money(), which
    # quantizes to cents and would silently turn 23.99% into 24%.
    apr: Decimal | None
    # What the household was actually paying on this card before we arrived — the
    # counterfactual the interest claim is measured against (prd.md §5.1). None when we
    # haven't observed enough history, in which case we make no claim at all rather than
    # falling back to the minimum, which would flatter us. See engine/interest.py.
    observed_monthly_payment: Decimal | None = None

    def __post_init__(self) -> None:
        """Corrupt debt data is rejected, not smoothed over.

        A negative minimum_payment — plausible from a misread signed Liabilities field —
        would shrink the reserve and hand the user a *larger* sweep. Clamping it to zero
        would stop the overdraft but silently invent a minimum of $0, which is the engine
        guessing at money. It refuses instead, and the sync layer has to deal with it.
        """
        if self.minimum_payment < ZERO:
            raise ValueError(f"minimum_payment={self.minimum_payment} cannot be negative")

        if self.balance < ZERO:
            raise ValueError(f"balance={self.balance} cannot be negative")

        if self.apr is not None and not (ZERO <= self.apr <= Decimal("2")):
            raise ValueError(f"apr={self.apr} is outside a plausible range (0–200%)")

        # A negative observed payment would make the counterfactual *cheaper* than reality
        # and inflate the interest we claim to have saved. Same rule as everywhere else:
        # bad upstream data must never buy us a bigger number.
        if self.observed_monthly_payment is not None and self.observed_monthly_payment < ZERO:
            raise ValueError(
                f"observed_monthly_payment={self.observed_monthly_payment} cannot be negative"
            )


class SpendChannel(str, Enum):
    """How money leaves — and *when*.

    The whole card model follows from one sentence: a card charge is not a checking outflow,
    it is a checking outflow *scheduled for the due date of the statement it lands on*.
    """

    CASH = "cash"  # debit, ACH, cash — leaves the funding account the day it posts
    CARD = "card"  # lands on a card ledger — leaves checking at that statement's due date


class Recurrence(str, Enum):
    RECURRING = "recurring"  # same merchant, monthly cadence, stable amount — rent, Netflix
    VARIABLE = "variable"  # regular but lumpy — groceries, fuel
    ONE_OFF = "one_off"  # the tail — a flight, a vet bill


class SpendCategory(str, Enum):
    """Plaid's Personal Finance Category taxonomy, not one of our own.

    A bespoke taxonomy would need a lossy mapping on the day real ingestion lands, and the
    mapping is where the category of a charge quietly changes meaning.
    """

    FOOD_AND_DRINK = "food_and_drink"
    GENERAL_MERCHANDISE = "general_merchandise"
    TRANSPORTATION = "transportation"
    TRAVEL = "travel"
    RENT_AND_UTILITIES = "rent_and_utilities"
    MEDICAL = "medical"
    PERSONAL_CARE = "personal_care"
    ENTERTAINMENT = "entertainment"
    GENERAL_SERVICES = "general_services"
    LOAN_PAYMENTS = "loan_payments"
    TRANSFER_OUT = "transfer_out"
    OTHER = "other"


class PaymentBehavior(str, Enum):
    """How the household settles this card. Learned from >= 3 observed cycles.

    Load-bearing in two places at once, which is why it is a stored field and not an
    inference made separately in each:

    - **The reserve.** A TRANSACTOR owes the whole statement on the due date. Reserving
      their $40 minimum against a $2,000 statement under-reserves by $1,960 and overdraws
      them.
    - **The claim.** A TRANSACTOR pays no interest at all — the grace period already does
      what our sweep claims to do. Their interest_avoided is $0, not a number. Sweeping
      their cash onto a card they were going to clear anyway is a *prepayment*, not a
      saving, and taking a share of it (prd.md §7.2) would be charging for nothing.
    """

    TRANSACTOR = "transactor"  # clears the statement; holds a grace period
    REVOLVER = "revolver"  # carries a balance; pays a habitual amount above the minimum
    MINIMUM_ONLY = "minimum_only"  # pays the minimum; may never amortize
    UNKNOWN = "unknown"  # < 3 cycles observed. We refuse rather than guess.


@dataclass(frozen=True)
class CardTransaction:
    """One posted charge or credit on a card ledger.

    Signed like every other amount in the engine: **negative is money out.** A charge is
    negative even though it *increases* what you owe, because the alternative — flipping the
    sign because the balance happens to be stored as a positive liability — is exactly the
    class of bug CashEvent.__post_init__ exists to catch. One rule, everywhere, or none.

    A charge is not a checking outflow. It is a checking outflow scheduled for the due date
    of the statement it lands on. Nothing in this codebase may treat it as same-day cash.
    """

    card_id: str
    posted_date: date
    amount: Decimal  # negative = a charge; positive = a refund or statement credit
    merchant: str
    category: SpendCategory
    recurrence: Recurrence

    def __post_init__(self) -> None:
        if self.amount == ZERO:
            raise ValueError("a card transaction of exactly zero is not a transaction")


@dataclass(frozen=True)
class StatementCycle:
    """The seam that turns spend into an obligation with a date.

    Getting the close date wrong by one day moves an entire month of spend across the 30-day
    horizon boundary — the difference between reserving for it and never seeing it. This is
    the most load-bearing arithmetic in the card model.
    """

    close_day_of_month: int
    # close -> due. Reg Z requires >= 21 on any card that charges interest.
    grace_days: int = MIN_GRACE_DAYS

    def __post_init__(self) -> None:
        if not (1 <= self.close_day_of_month <= 31):
            raise ValueError(
                f"close_day_of_month={self.close_day_of_month} is not a day of the month"
            )
        # Below the legal floor the due date lands earlier than it really can, pulling an
        # obligation out of the horizon and shrinking the reserve. Bad data must never buy a
        # bigger sweep — see docs/decision-engine.md §3.
        if self.grace_days < MIN_GRACE_DAYS:
            raise ValueError(
                f"grace_days={self.grace_days} is below the Reg Z floor of {MIN_GRACE_DAYS}"
            )

    def close_on_or_after(self, day: date) -> date:
        """The first statement close on or after `day`."""
        this_month = statement_day(day.year, day.month, self.close_day_of_month)
        if this_month >= day:
            return this_month
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        return statement_day(year, month, self.close_day_of_month)

    def due_for(self, close: date) -> date:
        """When the statement that closed on `close` must actually be paid."""
        return close + timedelta(days=self.grace_days)


@dataclass(frozen=True)
class Card:
    """A card the household is liable for.

    Carries **two** statements, and both are load-bearing:

    - The one that has **already closed**. Legally due on `statement_due_date`. A known fact,
      not a forecast.
    - The one that has **not closed yet**. `unbilled_balance` is what has posted since the
      last close; it closes on `next_close_date` and comes due a grace period after that.

    A reserve that looks only at the closed statement holds back nothing for the rest of the
    cycle — once the closed statement is paid, the next has not closed, and the card appears
    to owe nothing while charges pile up on it. `obligation_in_horizon()` reads both fields
    for that reason. See docs/plans/2026-07-14-001 and its Review History.
    """

    card_id: str
    # None when the issuer does not report it. Common, and the engine must not pretend
    # otherwise. A *rate*, not a dollar amount — never construct it with money().
    apr: Decimal | None
    cycle: StatementCycle

    # Already closed. Legally due on statement_due_date. A known fact, not a forecast.
    statement_balance: Decimal
    statement_due_date: date
    minimum_payment: Decimal

    # Posted since the last close. NOT yet due: these close on next_close_date and come due
    # a cycle after that. This is the number that determines *next month's* bill, and it is
    # the reason card transactions are an engine input at all.
    unbilled_balance: Decimal
    next_close_date: date

    behavior: PaymentBehavior = PaymentBehavior.UNKNOWN
    # What the household actually pays each cycle — the counterfactual the interest claim is
    # measured against (prd.md §5.1). Never the minimum: users of this product already pay
    # more than the minimum, which is *why* they have idle cash. See engine/interest.py.
    observed_monthly_payment: Decimal | None = None
    # What they *charge* to it each cycle. Without this the interest model projects a balance
    # that only ever shrinks, so a household whose card genuinely grows gets a payoff date that
    # never arrives and an interest-avoided figure overstated by construction — the very number
    # prd.md §5.1 says the company is graded on.
    #
    # `None` means we have not observed enough cycles to say, and the model then assumes zero
    # future charges. That is the *old* behaviour and it is the flattering one, so it is only
    # reachable while `PaymentBehavior.UNKNOWN` is already blocking the sweep outright.
    observed_monthly_charges: Decimal | None = None

    def __post_init__(self) -> None:
        """Corrupt card data is rejected, not smoothed over.

        Every check here refuses the direction that would *shrink the reserve*. A negative
        balance read from a misreported Liabilities field, a minimum below zero — each one
        hands the user a larger sweep than the truth licenses, which is the one failure this
        engine exists to prevent.
        """
        if self.statement_balance < ZERO:
            raise ValueError(f"statement_balance={self.statement_balance} cannot be negative")

        if self.unbilled_balance < ZERO:
            raise ValueError(f"unbilled_balance={self.unbilled_balance} cannot be negative")

        if self.minimum_payment < ZERO:
            raise ValueError(f"minimum_payment={self.minimum_payment} cannot be negative")

        if self.apr is not None and not (ZERO <= self.apr <= Decimal("2")):
            raise ValueError(f"apr={self.apr} is outside a plausible range (0–200%)")

        if self.observed_monthly_payment is not None and self.observed_monthly_payment < ZERO:
            raise ValueError(
                f"observed_monthly_payment={self.observed_monthly_payment} cannot be negative"
            )

        # A negative charge rate would *shrink* the projected balance, bring the payoff date
        # forward, and inflate the interest we claim to have saved. Same rule as everywhere.
        if self.observed_monthly_charges is not None and self.observed_monthly_charges < ZERO:
            raise ValueError(
                f"observed_monthly_charges={self.observed_monthly_charges} cannot be negative"
            )

        # The unbilled statement closes *after* the closed one came due. If these are the
        # wrong way round, the two are the same statement and it would be reserved twice —
        # or, worse, the "unbilled" figure is really an already-due one and gets treated as
        # a month further away than it is.
        if self.next_close_date < self.statement_due_date - timedelta(days=self.cycle.grace_days):
            raise ValueError(
                f"next_close_date={self.next_close_date} precedes the close of the statement "
                f"already due on {self.statement_due_date} — these are the same cycle"
            )

    @property
    def total_owed(self) -> Decimal:
        """Everything on this card: billed and not."""
        return self.statement_balance + self.unbilled_balance

    @property
    def interest_bearing_balance(self) -> Decimal:
        """The balance actually accruing interest today.

        A TRANSACTOR's unbilled charges accrue nothing — the grace period covers them, which
        is exactly what makes their interest_avoided $0 rather than a small number. A
        REVOLVER has lost the grace period, so their unbilled charges accrue from the day
        they posted.
        """
        if self.behavior is PaymentBehavior.TRANSACTOR:
            return self.statement_balance
        return self.total_owed


class CoverageState(str, Enum):
    COMPLETE = "complete"  # attested, and no unmatched card-shaped outflows
    UNMATCHED_PAYMENT = "unmatched_payment"  # we see a payment to a card we cannot see
    UNATTESTED = "unattested"  # the user has not confirmed this is all of them


@dataclass(frozen=True)
class UnmatchedPayment:
    """A recurring, card-shaped outflow that maps to no card we can see."""

    merchant: str
    typical_amount: Decimal
    day_of_month: int
    months_observed: int


@dataclass(frozen=True)
class CardPortfolio:
    """Every card the household is liable for — or an honest statement that we do not know.

    Optimality within an incomplete portfolio is not optimality. The engine already refuses
    to rank cards when an APR is missing, because paying the wrong card looks exactly like
    working while quietly destroying the entire point. This is that argument one level up,
    with a worse ending: paying the *right* card while starving a card we cannot see.

    That is not a bad sweep. It is an overdraft with a good explanation.
    """

    cards: tuple[Card, ...] = ()
    coverage: CoverageState = CoverageState.UNATTESTED
    unmatched_card_payments: tuple[UnmatchedPayment, ...] = ()

    def __post_init__(self) -> None:
        # COMPLETE means "we have looked and found nothing missing". Claiming it while
        # holding evidence of an unseen liability is the one contradiction that matters here:
        # it is the state in which the engine would sweep with full confidence into a card it
        # knows it cannot see.
        if self.coverage is CoverageState.COMPLETE and self.unmatched_card_payments:
            raise ValueError(
                f"coverage=COMPLETE contradicts {len(self.unmatched_card_payments)} unmatched "
                "card payment(s) — we cannot claim complete coverage and hold evidence against it"
            )

    @property
    def is_complete(self) -> bool:
        return self.coverage is CoverageState.COMPLETE


@dataclass(frozen=True)
class RecurringCommitment:
    """The floor a household cannot flex. Rent, insurance, subscriptions."""

    merchant: str
    category: SpendCategory
    channel: SpendChannel
    typical_amount: Decimal
    amount_p90: Decimal
    day_of_month: int
    months_observed: int


@dataclass(frozen=True)
class CategoryStats:
    median_monthly: Decimal
    p90_monthly: Decimal
    worst_month: Decimal
    months_observed: int


@dataclass(frozen=True)
class SpendProfile:
    """What normal looks like for this household, over 12 months, across every channel.

    Non-parametric by construction, and the 2026-07-13 learning is explicit about why: real
    spend is zero-inflated and right-skewed, sigma is a poor description of its tail, and a
    parametric `mu + z*sigma*sqrt(t)` would reintroduce the same class of error the current
    model has ("variance grows with sqrt(t), and this model grows it with t"). So we
    enumerate the household's own overlapping 30-day windows and read the quantile straight
    off them.

    Deterministic (the "sampling" is their own history, already in the Snapshot), uses their
    real skew and autocorrelation, and explainable in one sentence: *"your worst 30-day
    stretch last year was $2,231."*

    **This feeds no decision.** It is a structure and a dashboard. Swapping the forecast onto
    its empirical quantile would *loosen* the reserve, and loosening requires the measured
    breach rate that engine/outcome.py cannot yet produce. See the plan's U8.
    """

    window_days: int = 365
    commitments: tuple[RecurringCommitment, ...] = ()
    by_category: Mapping[SpendCategory, CategoryStats] = field(default_factory=dict)
    # Every overlapping 30-day total in the trailing year, split by channel. The dashboard
    # renders these. The forecast will eventually read its quantile off them — but not yet.
    rolling_30d_cash: tuple[Decimal, ...] = ()
    rolling_30d_card: tuple[Decimal, ...] = ()

    def __post_init__(self) -> None:
        if self.window_days <= 0:
            raise ValueError(f"window_days={self.window_days} must be positive")

    @property
    def worst_30d_cash(self) -> Decimal:
        return max(self.rolling_30d_cash, default=ZERO)

    @property
    def worst_30d_card(self) -> Decimal:
        return max(self.rolling_30d_card, default=ZERO)


@dataclass(frozen=True)
class UserPolicy:
    """User-set guardrails. The engine may never exceed these, only fall short."""

    buffer_floor: Decimal
    max_sweep: Decimal
    max_weekly_sweep: Decimal
    blackout_dates: frozenset[date] = field(default_factory=frozenset)
    # The minimum spacing between two ACH debits. Every sweep is an independent draw from
    # the forecast-error distribution, and prd.md §2 says we win this on the tail, not on
    # expected value — so the number of draws is itself a risk control, not a UX detail.
    # A spacing rule rather than a calendar: a day we hold for lack of surplus does not
    # push the next eligible day out, so the household never loses a cycle to a quiet week.
    min_days_between_sweeps: int = 7

    def __post_init__(self) -> None:
        # Zero is the old daily behaviour and remains expressible; negative is nonsense and
        # would read as "sweep more often than every day", which is not a thing.
        if self.min_days_between_sweeps < 0:
            raise ValueError(
                f"min_days_between_sweeps={self.min_days_between_sweeps} cannot be negative"
            )


@dataclass(frozen=True)
class Snapshot:
    """Everything the engine is allowed to look at, frozen at a point in time.

    The whole input is captured with each decision so any past sweep can be replayed
    and explained even after Plaid mutates the underlying history beneath us.
    """

    today: date
    accounts: tuple[Account, ...]
    # The account the ACH debit actually leaves from. Only *this* balance protects the
    # user from an overdraft — see forecast.conservative_low_balance.
    funding_account_id: str
    events: tuple[CashEvent, ...]
    pending: tuple[PendingTransaction, ...]
    # Every card the household is liable for — or an honest statement that we do not know.
    #
    # This replaced `debts: tuple[Debt, ...]`, and the replacement is the feature. A `Debt`
    # knows only what the *issuer* will accept (the minimum). A `Card` knows what the
    # *household* will pay, which for two of the three behaviours is a completely different
    # number — and reserving the first while the second leaves checking is the overdraft this
    # engine exists to prevent.
    portfolio: CardPortfolio
    policy: UserPolicy
    # p90 of daily discretionary spend — the high end, deliberately.
    daily_discretionary_high: Decimal
    # Coefficient of variation of observed monthly income. High = unforecastable.
    income_variation: float
    # Days of transaction history we actually have.
    history_days: int
    # Sum of sweeps initiated but not yet settled — money already gone that the
    # bank may not have subtracted from the balance yet.
    sweeps_in_flight: Decimal = ZERO
    swept_this_week: Decimal = ZERO
    # Days since the last sweep actually left. None means we have never swept for this
    # household, which is the one honest way to be eligible on day one.
    #
    # Note the default is the *permissive* value, unlike everywhere else in this file. It is
    # defensible only because the cadence is a frequency control, not a solvency gate: a
    # sync bug that leaves this unset produces sweeps that are each still individually safe
    # (the buffer and the forecast are untouched), it just produces more of them — i.e. it
    # degrades to the behaviour the engine shipped with. It cannot make any single sweep
    # larger, which is the thing [3] actually forbids.
    days_since_last_sweep: int | None = None

    def __post_init__(self) -> None:
        if self.days_since_last_sweep is not None and self.days_since_last_sweep < 0:
            raise ValueError(
                f"days_since_last_sweep={self.days_since_last_sweep} cannot be negative"
            )


class Action(str, Enum):
    SWEEP = "sweep"
    REFUSE = "refuse"


class ReasonCode(str, Enum):
    """Why the engine did what it did.

    Codes, not sentences. A reason is a fact about the decision; the sentence is one
    rendering of that fact. Keeping them apart means copy edits can't break the test
    suite, the audit log stays stable while the UI changes, translation is possible at
    all, and the LLM has something to narrate *from* rather than merely passing through.

    See engine/explain.py for the rendering.
    """

    # Blocking — no amount of surplus justifies moving money.
    FUNDING_ACCOUNT_MISSING = "funding_account_missing"
    FUNDING_ACCOUNT_NOT_CHECKING = "funding_account_not_checking"
    CONNECTION_UNHEALTHY = "connection_unhealthy"
    BALANCE_STALE = "balance_stale"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOME_TOO_VARIABLE = "income_too_variable"
    BLACKOUT = "blackout"
    SWEEP_IN_FLIGHT = "sweep_in_flight"

    # We cannot see the whole liability, so we will not act on part of it. Sweeping optimally
    # into a portfolio we cannot see is not optimality — it is an overdraft with a good
    # explanation.
    CARD_COVERAGE_INCOMPLETE = "card_coverage_incomplete"
    # Fewer than three observed cycles. We do not know what this card will take out of
    # checking, and guessing sets both the reserve and the interest claim.
    CARD_BEHAVIOR_UNKNOWN = "card_behavior_unknown"

    # Nothing to aim at.
    NO_DEBT = "no_debt"
    APR_UNKNOWN = "apr_unknown"
    # Every card they hold is a transactor's. The grace period already does what our sweep
    # claims to do, so there is no interest for us to avoid and nothing honest to charge for.
    NO_INTEREST_TO_AVOID = "no_interest_to_avoid"

    # The money isn't there.
    NO_SURPLUS = "no_surplus"
    BELOW_MIN_SWEEP = "below_min_sweep"

    # The money is there, and we are choosing not to move it *today*. Deliberately not a
    # blocking gate: it is raised only after the forecast has run, so the day still carries
    # a projection and stays gradeable. See decide() and decision-engine.md [9].
    CADENCE_HOLD = "cadence_hold"

    # Why this amount, and not more.
    PROJECTION = "projection"
    # The cash is smaller than the balance suggests because a statement is already spoken for.
    STATEMENT_RESERVED = "statement_reserved"
    PER_SWEEP_CAP = "per_sweep_cap"
    WEEKLY_CAP = "weekly_cap"
    CLEARS_THE_CARD = "clears_the_card"

    # Advisory — true, and worth saying, but not why we acted.
    IDLE_CASH_ELSEWHERE = "idle_cash_elsewhere"
    # Emitted only when the APR is known *and* the debt actually amortizes. Its absence is
    # how the engine declines to make a claim it cannot stand behind — see engine/interest.py.
    INTEREST_AVOIDED = "interest_avoided"
    # What has been charged this cycle and when it comes due. Not why we acted — but it is the
    # number that determines next month's bill, and the user cannot see it anywhere else.
    UNBILLED_ACCRUING = "unbilled_accruing"


@dataclass(frozen=True)
class Reason:
    code: ReasonCode
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    action: Action
    amount: Decimal
    target_debt_id: str | None
    # Structured facts about why. Render with engine.explain.explain().
    reasons: tuple[Reason, ...]
    projected_low_balance: Decimal | None = None

    @property
    def swept(self) -> bool:
        return self.action is Action.SWEEP

    @property
    def codes(self) -> tuple[ReasonCode, ...]:
        return tuple(r.code for r in self.reasons)

    def has(self, code: ReasonCode) -> bool:
        return code in self.codes
