"""IncreaseProvider — the ACH debit leg (ticket 0041, U3).

Translates the `TransferProvider` port to Increase's ACH Transfers API. Research pinned the shapes
that a naive port would get wrong (KTD-3):

- **Debit is a negative signed amount**, not a direction field: `POST /ach_transfers` takes a signed
  `amount` and there is no `direction`. `signed_amount_cents` is the one place that sign is applied.
- **Increase has no `settled` status.** Settlement is *derived*: a `submitted` transfer whose
  associated Transaction exists and for which no return has arrived within the window is settled.
  `map_status` imposes that — the ledger's `settled` is ours, not something Increase hands back.
- **Returns** arrive as a separate event carrying a NACHA `return_reason_code` (R01/R02/R03…).

Funded by Plaid **Auth** account/routing numbers — Increase is *not* a Plaid processor partner, so
the brainstorm's processor-token handoff does not apply (that path is Dwolla's). `counterparty` is
where those Auth numbers become Increase's counterparty shape.

The live HTTP client is wired and exercised in U6 against Increase's Sandbox. Everything here is the
translation — pure, unit-tested behind a fake client — because that is where vendors surprise you.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from backend.transfer.provider import (
    Auth,
    Direction,
    LedgerState,
    ProviderRef,
    Return,
    TransferIntent,
)


@dataclass(frozen=True)
class AuthNumbers:
    """A funding source from Plaid Auth (`/auth/get`) — the numbers Increase debits."""

    account_number: str
    routing_number: str


def counterparty(numbers: AuthNumbers) -> dict[str, str]:
    """Plaid Auth numbers in Increase's counterparty shape. A dedicated External Account resource is
    the alternative; the inline numbers are the smaller first step."""
    return {"account_number": numbers.account_number, "routing_number": numbers.routing_number}


def signed_amount_cents(direction: Direction, amount: Decimal) -> int:
    """Cents, signed the way Increase wants: **negative for a debit** (a pull), positive to credit.

    The whole-cents conversion goes through the exact `Decimal`, never a float — a debit that
    round-trips through binary is no longer the cent the engine decided on (ADR-0002 [2.2])."""
    cents = int((amount * 100).to_integral_value())
    return -cents if direction == "debit" else cents


# Increase's transfer statuses → our ledger vocabulary. `submitted` is not terminal-settled; see
# `map_status`, which derives settlement Increase never reports as an enum.
_STATUS: dict[str, LedgerState] = {
    "pending_approval": "pending",
    "pending_transfer_session_confirmation": "pending",
    "pending_submission": "pending",
    "pending_reviewing": "pending",
    "submitted": "submitted",
    "requires_attention": "pending",  # stuck, not failed — reconciliation/operator resolves it
    "rejected": "failed",
    "canceled": "cancelled",
    "returned": "returned",
}


def map_status(increase_status: str, *, transaction_exists: bool, returned: bool) -> LedgerState:
    """Map an Increase transfer to our `LedgerState`, deriving the `settled` Increase never sends.

    A return wins outright. Otherwise a `submitted` transfer whose Transaction has posted and which
    has not returned is what we call `settled` — a state our machine imposes on observation, per
    KTD-3.
    """
    if returned:
        return "returned"
    base = _STATUS.get(increase_status, "pending")
    if base == "submitted" and transaction_exists:
        return "settled"
    return base


@dataclass(frozen=True)
class IncreaseTransferView:
    """What `get_transfer` returns — the facts `map_status` joins."""

    status: str
    transaction_exists: bool
    return_code: str | None


class IncreaseClient(Protocol):
    """The narrow slice of Increase's API this leg uses. The live implementation (U6) wraps the SDK;
    tests supply a fake. Kept a Protocol so no HTTP library is imported at the port layer."""

    def create_ach_transfer(
        self,
        *,
        account_id: str,
        amount_cents: int,
        account_number: str,
        routing_number: str,
        idempotency_key: str,
        statement_descriptor: str,
    ) -> str:
        """Create the transfer; return Increase's transfer id."""
        ...

    def get_transfer(self, transfer_id: str) -> IncreaseTransferView: ...


@dataclass(frozen=True)
class IncreaseProvider:
    """The debit-leg adapter. Needs the platform funding account (funds land there) and a resolver
    for the user's Auth numbers to debit — Plaid `/auth/get` live, a fake in tests. Keeping the
    resolver internal is what lets `submit(auth)` still satisfy the port (KTD-3); the numbers are
    not in the intent, and are not vendor detail the shared `Auth` type should carry."""

    client: IncreaseClient
    funding_account_id: str
    numbers_for: Callable[[str], AuthNumbers]
    statement_descriptor: str = "RESFI SWEEP"

    def authorize(self, intent: TransferIntent) -> Auth:
        # Client-side: derive the idempotency key from the slot + step (KTD-4). No vendor call.
        key = f"{intent.household_id}-{intent.decision_date.isoformat()}-{intent.leg}-submit"
        return Auth(intent=intent, idempotency_key=key)

    def submit(self, auth: Auth) -> ProviderRef:
        cp = counterparty(self.numbers_for(auth.intent.household_id))
        transfer_id = self.client.create_ach_transfer(
            account_id=self.funding_account_id,
            amount_cents=signed_amount_cents(auth.intent.direction, auth.intent.amount),
            account_number=cp["account_number"],
            routing_number=cp["routing_number"],
            idempotency_key=auth.idempotency_key,
            statement_descriptor=self.statement_descriptor,
        )
        return ProviderRef(provider_transfer_id=transfer_id)

    def status(self, ref: ProviderRef) -> LedgerState:
        view = self.client.get_transfer(ref.provider_transfer_id)
        return map_status(
            view.status,
            transaction_exists=view.transaction_exists,
            returned=view.return_code is not None,
        )

    def handle_return(self, ref: ProviderRef) -> Return | None:
        view = self.client.get_transfer(ref.provider_transfer_id)
        if view.return_code is None:
            return None
        return Return(return_code=view.return_code, state="returned")
