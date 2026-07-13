"""Value types for the sweep decision engine.

Money is `Decimal` quantized to cents everywhere. No floats touch a dollar amount.
Dates are the *user's* local dates, always passed in explicitly — the engine never
reads a clock. See docs/decision-engine.md [2.1] for why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


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

    The recurring-event detector will happily identify a credit card's minimum payment
    as a monthly obligation — it looks exactly like one. But `Debt` already carries that
    minimum explicitly, and `decide()` reserves it out of available cash. Left
    undistinguished, the same payment is subtracted twice: once by the forecast, once by
    the reserve.

    The direction is safe (we under-sweep, nobody is overdrawn), which is precisely why
    it would have gone unnoticed — the product would just quietly refuse more often than
    it should, forever.

    `Debt` is the authoritative source for minimums. The forecast skips DEBT_MINIMUM
    events and lets the reserve do that job alone.
    """

    ORDINARY = "ordinary"
    DEBT_MINIMUM = "debt_minimum"


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
    # DEBT_MINIMUM events are excluded from the forecast — Debt.minimum_payment is the
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


@dataclass(frozen=True)
class UserPolicy:
    """User-set guardrails. The engine may never exceed these, only fall short."""

    buffer_floor: Decimal
    max_sweep: Decimal
    max_weekly_sweep: Decimal
    blackout_dates: frozenset[date] = field(default_factory=frozenset)


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
    debts: tuple[Debt, ...]
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

    # Nothing to aim at.
    NO_DEBT = "no_debt"
    APR_UNKNOWN = "apr_unknown"

    # The money isn't there.
    NO_SURPLUS = "no_surplus"
    BELOW_MIN_SWEEP = "below_min_sweep"

    # Why this amount, and not more.
    PROJECTION = "projection"
    PER_SWEEP_CAP = "per_sweep_cap"
    WEEKLY_CAP = "weekly_cap"
    CLEARS_THE_CARD = "clears_the_card"

    # Advisory — true, and worth saying, but not why we acted.
    IDLE_CASH_ELSEWHERE = "idle_cash_elsewhere"
    # Emitted only when the APR is known *and* the debt actually amortizes. Its absence is
    # how the engine declines to make a claim it cannot stand behind — see engine/interest.py.
    INTEREST_AVOIDED = "interest_avoided"


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
