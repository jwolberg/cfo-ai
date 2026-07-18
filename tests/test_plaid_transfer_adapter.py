"""The Plaid Transfer debit adapter (ticket 0045, ADR-0007).

The primary debit rail. Two things differ from Increase and are worth pinning: `authorize()` is a
real two-step vendor call (authorization → transfer), and `settled` is reported directly (no
derivation). Pure + fake-client; the live HTTP is U6's sandbox gate.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from backend.transfer import PlaidItem, PlaidTransferProvider, TransferIntent, TransferProvider
from backend.transfer.plaid_transfer import PlaidTransferView, amount_str, map_status


def _intent() -> TransferIntent:
    return TransferIntent(
        household_id="h1",
        target_card_id="card-1",
        decision_id="dec-1",
        decision_date=dt.date(2026, 3, 2),
        leg="debit",
        direction="debit",
        amount=Decimal("200.00"),
        provider="plaid_transfer",
    )


def test_amount_is_a_two_decimal_string_from_the_exact_decimal() -> None:
    assert amount_str(Decimal("200.00")) == "200.00"
    assert amount_str(Decimal("1234.5")) == "1234.50"


def test_settled_is_reported_directly_not_derived() -> None:
    assert map_status("settled") == "settled"
    assert map_status("funds_available") == "settled"
    assert map_status("pending") == "pending"
    assert map_status("posted") == "pending"
    assert map_status("returned") == "returned"
    assert map_status("failed") == "failed"
    assert map_status("cancelled") == "cancelled"
    assert map_status("who_knows") == "pending"  # never a silent settled


class _FakePlaidTransfer:
    def __init__(self, status: str = "settled", return_code: str | None = None) -> None:
        self._status = status
        self._return_code = return_code
        self.calls: dict[str, object] = {}

    def authorize(self, *, access_token, account_id, amount, idempotency_key):  # noqa: ANN001, ANN003
        self.calls["authorize"] = {
            "access_token": access_token,
            "account_id": account_id,
            "amount": amount,
            "idempotency_key": idempotency_key,
        }
        return "auth_123"

    def create_transfer(  # noqa: ANN001, ANN201
        self, *, authorization_id, amount, description, idempotency_key
    ):
        self.calls["create"] = {
            "authorization_id": authorization_id,
            "amount": amount,
            "idempotency_key": idempotency_key,
        }
        return "tfr_123"

    def get_transfer(self, transfer_id):  # noqa: ANN001, ANN201
        return PlaidTransferView(status=self._status, return_code=self._return_code)


def _provider(client: _FakePlaidTransfer) -> PlaidTransferProvider:
    return PlaidTransferProvider(
        client=client,
        item_for=lambda _h: PlaidItem(access_token="access-sandbox-x", account_id="acc_1"),
    )


def test_the_adapter_satisfies_the_port() -> None:
    assert isinstance(_provider(_FakePlaidTransfer()), TransferProvider)


def test_authorize_is_a_vendor_call_that_carries_the_authorization_ref() -> None:
    """Unlike Increase/Method, Plaid Transfer authorizes at the vendor first; the id rides Auth."""
    client = _FakePlaidTransfer()
    auth = _provider(client).authorize(_intent())
    assert auth.authorization_ref == "auth_123"
    assert auth.idempotency_key == "h1-2026-03-02-debit-submit"
    assert client.calls["authorize"]["access_token"] == "access-sandbox-x"
    assert client.calls["authorize"]["amount"] == "200.00"


def test_submit_uses_the_authorization_ref_and_the_slot_key() -> None:
    client = _FakePlaidTransfer()
    provider = _provider(client)
    ref = provider.submit(provider.authorize(_intent()))
    assert ref.provider_transfer_id == "tfr_123"
    assert client.calls["create"]["authorization_id"] == "auth_123"
    assert client.calls["create"]["idempotency_key"] == "h1-2026-03-02-debit-submit"
    assert provider.status(ref) == "settled"


def test_a_return_surfaces_its_code() -> None:
    client = _FakePlaidTransfer(status="returned", return_code="R01")
    provider = _provider(client)
    ret = provider.handle_return(provider.submit(provider.authorize(_intent())))
    assert ret is not None
    assert ret.return_code == "R01"
    assert ret.state == "returned"
