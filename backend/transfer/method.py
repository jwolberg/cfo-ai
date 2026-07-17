"""MethodProvider — the card payoff leg (ticket 0041, U3).

Translates the `TransferProvider` port to Method's Payments API. Research pinned the shapes (KTD-3):

- **`submit` = `POST /payments`** from a platform `source` to a `destination` liability, with a
  `description` capped at **10 characters** (tighter than Increase's descriptor).
- **Source must be a platform funding account**; an end-user's checking cannot be a source — the
  reason the money transits an FBO ([5.2], ADR-0006). **Destination must be a `liability`** account
  (the card); `validate_destination` refuses anything else.
- **Status** runs `pending → processing → sent → posted`; a reversal is *further status on the same
  payment* (`reversal_processing → reversed`), not a separate resource — unlike Increase, whose
  return is a distinct event. `map_status` normalizes both into our vocabulary.
- **Idempotency** replays the original response (a reused key does not 409 as Increase's does), so
  the per-vendor conflict handling differs — the saga (U4) owns that; the key derivation is shared.

The tenant-wide API key is guarded from day one (`assert_transfer_credentials_safe_at_rest`,
secrets manager only) and never logged. The live client lands in U6; the translation is here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from backend.transfer.provider import (
    Auth,
    LedgerState,
    ProviderRef,
    Return,
    TransferIntent,
)

# Method's liability types that can be a payoff destination (credit card is the one this rung uses).
_LIABILITY_TYPES = frozenset(
    {"credit_card", "loan", "mortgage", "student_loan", "auto_loan", "liability"}
)


class NotAPayableDestination(ValueError):
    """Raised when a payoff would target something that is not a Method liability account."""


@dataclass(frozen=True)
class MethodDestination:
    """A destination discovered through Method Connect: the account id and its type."""

    account_id: str
    account_type: str


def validate_destination(destination: MethodDestination) -> None:
    """A payoff may only land on a liability (the card). A funding/depository destination is a bug —
    paying money *into* a checking account is not paying down a card."""
    if destination.account_type not in _LIABILITY_TYPES:
        raise NotAPayableDestination(
            f"payoff destination {destination.account_id} is {destination.account_type!r}, not a "
            f"liability; a card payoff must target one of {sorted(_LIABILITY_TYPES)}"
        )


def amount_cents(amount: Decimal) -> int:
    """Whole cents from the exact `Decimal` — never a float (ADR-0002 [2.2])."""
    return int((amount * 100).to_integral_value())


def description(raw: str = "RESFI") -> str:
    """A payment description within Method's 10-character cap."""
    return raw[:10]


# Method's payment statuses → our ledger vocabulary. A reversal in any of its forms unwinds, which
# is the safe direction: it stops the next sweep from stacking (`SWEEP_IN_FLIGHT`, U5).
_STATUS: dict[str, LedgerState] = {
    "pending": "pending",
    "processing": "pending",
    "sent": "pending",
    "posted": "settled",
    "failed": "failed",
    "canceled": "cancelled",
    "reversal_required": "returned",
    "reversal_processing": "returned",
    "reversed": "returned",
}


def map_status(method_status: str) -> LedgerState:
    """Map a Method payment status to our `LedgerState`. Unknown statuses stay `pending` — never a
    silent `settled`, because the thing that would be wrong is a card payment reported as done."""
    return _STATUS.get(method_status, "pending")


@dataclass(frozen=True)
class MethodPaymentView:
    """What `get_payment` returns."""

    status: str
    error_code: str | None


class MethodClient(Protocol):
    """The narrow slice of Method's Payments API this leg uses. Live implementation in U6; a fake in
    tests. The API key it carries is never logged (KTD-7)."""

    def create_payment(
        self,
        *,
        source_id: str,
        destination_id: str,
        amount_cents: int,
        description: str,
        idempotency_key: str,
    ) -> str:
        """Create the payment; return Method's payment id."""
        ...

    def get_payment(self, payment_id: str) -> MethodPaymentView: ...


@dataclass(frozen=True)
class MethodProvider:
    """The payoff-leg adapter. Pays from the platform source to the card's Method liability,
    resolved from the target card by a discovery function (Method Connect live, a fake in tests)."""

    client: MethodClient
    source_id: str
    destination_for: Callable[[str], MethodDestination]

    def authorize(self, intent: TransferIntent) -> Auth:
        key = f"{intent.household_id}-{intent.decision_date.isoformat()}-{intent.leg}-submit"
        return Auth(intent=intent, idempotency_key=key)

    def submit(self, auth: Auth) -> ProviderRef:
        destination = self.destination_for(auth.intent.target_card_id)
        validate_destination(destination)  # never pay a non-liability
        payment_id = self.client.create_payment(
            source_id=self.source_id,
            destination_id=destination.account_id,
            amount_cents=amount_cents(auth.intent.amount),
            description=description(),
            idempotency_key=auth.idempotency_key,
        )
        return ProviderRef(provider_transfer_id=payment_id)

    def status(self, ref: ProviderRef) -> LedgerState:
        return map_status(self.client.get_payment(ref.provider_transfer_id).status)

    def handle_return(self, ref: ProviderRef) -> Return | None:
        view = self.client.get_payment(ref.provider_transfer_id)
        state = map_status(view.status)
        if state != "returned":
            return None
        return Return(return_code=view.error_code or view.status, state="returned")
