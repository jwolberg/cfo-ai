"""PlaidTransferProvider — the ACH debit leg on Plaid Transfer (ticket 0045, ADR-0007).

The primary debit rail, replacing Increase. It debits off the Plaid item the transport rung already
linked (no Auth-number handoff — Increase needed one because it is not a Plaid processor partner).
It is multi-rail, and exposes Plaid Signal for the timing model's return-risk score.

Two shapes that differ from Increase (ADR-0007 [2.1]):

- **`authorize()` is a real vendor call.** Plaid Transfer requires `/transfer/authorization/create`
  before `/transfer/create`; the authorization id rides on `Auth.authorization_ref` into `submit()`.
- **`settled` is reported directly** — Plaid Transfer has a real `settled` status, so no settlement
  is derived (contrast Increase's `map_status`).

The live HTTP client lands with U6's sandbox run; the translation — the part vendors surprise you on
— is pure and unit-tested behind a fake client.
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

PROVIDER_NAME = "plaid_transfer"


@dataclass(frozen=True)
class PlaidItem:
    """The linked-item facts Plaid Transfer needs to debit — resolved from the household."""

    access_token: str
    account_id: str


def amount_str(amount: Decimal) -> str:
    """Plaid Transfer takes the amount as a decimal string (e.g. "200.00"), not integer cents — and
    from the exact `Decimal`, never a float (ADR-0002 [2.2])."""
    return f"{amount:.2f}"


# Plaid Transfer's `transfer.status` → our ledger vocabulary. Unlike Increase, `settled` is reported
# directly, so nothing is derived. Unknown stays `pending` — never a silent `settled`.
_STATUS: dict[str, LedgerState] = {
    "pending": "pending",
    "posted": "pending",
    "settled": "settled",
    "funds_available": "settled",
    "returned": "returned",
    "cancelled": "cancelled",
    "failed": "failed",
}


def map_status(plaid_status: str) -> LedgerState:
    return _STATUS.get(plaid_status, "pending")


@dataclass(frozen=True)
class PlaidTransferView:
    """What `get_transfer` returns."""

    status: str
    return_code: str | None


class PlaidTransferClient(Protocol):
    """The slice of Plaid Transfer this leg uses. Live implementation in U6; a fake in tests. The
    access_token it carries is never logged."""

    def authorize(
        self, *, access_token: str, account_id: str, amount: str, idempotency_key: str
    ) -> str:
        """`/transfer/authorization/create`; return the authorization id (raises if declined)."""
        ...

    def create_transfer(
        self, *, authorization_id: str, amount: str, description: str, idempotency_key: str
    ) -> str:
        """`/transfer/create`; return Plaid's transfer id."""
        ...

    def get_transfer(self, transfer_id: str) -> PlaidTransferView: ...


@dataclass(frozen=True)
class PlaidTransferProvider:
    """The debit-leg adapter on Plaid Transfer. Resolves the user's Plaid item (access_token +
    account id) from the household via an injected resolver — Plaid `/item` data live, a fake in
    tests — so `submit(auth)` still satisfies the port."""

    client: PlaidTransferClient
    item_for: Callable[[str], PlaidItem]
    description: str = "RESFI SWEEP"

    def authorize(self, intent: TransferIntent) -> Auth:
        # A real vendor call: Plaid Transfer authorizes before it creates (ADR-0007). The
        # idempotency key still derives from the slot + step (KTD-4) and is reused on retry.
        key = f"{intent.household_id}-{intent.decision_date.isoformat()}-{intent.leg}-submit"
        item = self.item_for(intent.household_id)
        authorization_id = self.client.authorize(
            access_token=item.access_token,
            account_id=item.account_id,
            amount=amount_str(intent.amount),
            idempotency_key=f"{key}-authorize",
        )
        return Auth(intent=intent, idempotency_key=key, authorization_ref=authorization_id)

    def submit(self, auth: Auth) -> ProviderRef:
        assert auth.authorization_ref is not None, (
            "Plaid Transfer submit needs an authorization ref"
        )
        transfer_id = self.client.create_transfer(
            authorization_id=auth.authorization_ref,
            amount=amount_str(auth.intent.amount),
            description=self.description,
            idempotency_key=auth.idempotency_key,
        )
        return ProviderRef(provider_transfer_id=transfer_id)

    def status(self, ref: ProviderRef) -> LedgerState:
        return map_status(self.client.get_transfer(ref.provider_transfer_id).status)

    def handle_return(self, ref: ProviderRef) -> Return | None:
        view = self.client.get_transfer(ref.provider_transfer_id)
        if map_status(view.status) != "returned":
            return None
        return Return(return_code=view.return_code or view.status, state="returned")
