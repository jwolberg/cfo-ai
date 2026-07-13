"""Value types for the sweep decision engine.

Money is `Decimal` quantized to cents everywhere. No floats touch a dollar amount.
Dates are the *user's* local dates, always passed in explicitly — the engine never
reads a clock. See docs/decision-engine.md [2.1] for why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: str | int | Decimal) -> Decimal:
    """Quantize to cents. The only way a Decimal amount should be constructed."""
    return Decimal(value).quantize(CENTS)


class ConnectionState(str, Enum):
    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class Account:
    account_id: str
    balance: Decimal
    connection: ConnectionState
    # Days since the balance was last confirmed with the institution.
    balance_age_days: int


@dataclass(frozen=True)
class CashEvent:
    """A predicted future inflow or outflow.

    `amount` is signed: positive is money in, negative is money out.

    `amount_low` / `amount_high` bound the plausible magnitude, and `date_jitter_days`
    bounds the timing. The forecast uses whichever end of each range is *worse* for the
    user — see forecast.conservative_low_balance.
    """

    label: str
    expected_date: date
    amount: Decimal
    amount_low: Decimal  # smallest plausible magnitude (still signed)
    amount_high: Decimal  # largest plausible magnitude (still signed)
    date_jitter_days: int
    confidence: float  # P(this event occurs roughly as predicted), 0..1

    @property
    def is_inflow(self) -> bool:
        return self.amount > ZERO


@dataclass(frozen=True)
class PendingTransaction:
    """Authorized but not yet posted. Signed like CashEvent."""

    label: str
    amount: Decimal


@dataclass(frozen=True)
class Debt:
    debt_id: str
    balance: Decimal
    minimum_payment: Decimal
    minimum_due_date: date
    # None when the issuer does not report it through Plaid. This is common, and the
    # engine must not pretend otherwise. See docs/decision-engine.md [4.3].
    apr: Decimal | None


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


@dataclass(frozen=True)
class Decision:
    action: Action
    amount: Decimal
    target_debt_id: str | None
    # Every reason, in plain language, that produced this outcome. This is the
    # explanation surface: the LLM narrates these, it does not generate them.
    reasons: tuple[str, ...]
    projected_low_balance: Decimal | None = None

    @property
    def swept(self) -> bool:
        return self.action is Action.SWEEP
