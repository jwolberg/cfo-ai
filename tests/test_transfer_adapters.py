"""The Increase and Method adapters' translation logic (ticket 0041, U3).

The part research said vendors surprise you on (KTD-3): Increase's signed-amount debit and derived
settlement, Method's liability-only destination and reversal-as-status. All pure or fake-client —
the live HTTP is U6's Sandbox hard gate.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from backend.transfer import (
    AuthNumbers,
    IncreaseProvider,
    MethodDestination,
    MethodProvider,
    TransferIntent,
    TransferProvider,
)
from backend.transfer.increase import (
    IncreaseTransferView,
    counterparty,
    signed_amount_cents,
)
from backend.transfer.increase import (
    map_status as increase_map_status,
)
from backend.transfer.method import (
    MethodPaymentView,
    NotAPayableDestination,
    amount_cents,
    description,
    validate_destination,
)
from backend.transfer.method import (
    map_status as method_map_status,
)


def _intent(leg: str = "debit", provider: str = "increase") -> TransferIntent:
    return TransferIntent(
        household_id="h1",
        target_card_id="card-1",
        decision_id="dec-1",
        decision_date=dt.date(2026, 3, 2),
        leg=leg,  # type: ignore[arg-type]
        direction="debit" if leg == "debit" else "credit",  # type: ignore[arg-type]
        amount=Decimal("50.00"),
        provider=provider,  # type: ignore[arg-type]
    )


# --- Increase: the debit leg -------------------------------------------------------------------


def test_a_debit_is_a_negative_signed_amount_in_cents() -> None:
    """Increase has no direction field — a debit is a negative amount (KTD-3), and the cents come
    from the exact Decimal, never a float."""
    assert signed_amount_cents("debit", Decimal("50.00")) == -5000
    assert signed_amount_cents("credit", Decimal("50.00")) == 5000
    assert signed_amount_cents("debit", Decimal("1234.56")) == -123456


def test_plaid_auth_numbers_become_the_increase_counterparty_shape() -> None:
    numbers = AuthNumbers(account_number="000123456789", routing_number="021000021")
    assert counterparty(numbers) == {
        "account_number": "000123456789",
        "routing_number": "021000021",
    }


def test_increase_settlement_is_derived_because_there_is_no_settled_status() -> None:
    """A submitted transfer with a posted Transaction and no return is what *we* call settled."""
    assert increase_map_status("submitted", transaction_exists=True, returned=False) == "settled"
    # Submitted but the Transaction has not posted yet — still in flight, not settled.
    assert increase_map_status("submitted", transaction_exists=False, returned=False) == "submitted"
    # A return wins outright, whatever the transfer status says.
    assert increase_map_status("submitted", transaction_exists=True, returned=True) == "returned"
    assert increase_map_status("rejected", transaction_exists=False, returned=False) == "failed"
    assert increase_map_status("canceled", transaction_exists=False, returned=False) == "cancelled"


def test_the_increase_adapter_submits_and_derives_status_through_a_fake_client() -> None:
    created: dict[str, object] = {}

    class _FakeIncrease:
        def create_ach_transfer(self, **kwargs):  # noqa: ANN003, ANN201
            created.update(kwargs)
            return "ach_transfer_123"

        def get_transfer(self, transfer_id):  # noqa: ANN001, ANN201
            return IncreaseTransferView(
                status="submitted", transaction_exists=True, return_code=None
            )

    provider = IncreaseProvider(
        client=_FakeIncrease(),
        funding_account_id="acct_platform",
        numbers_for=lambda _h: AuthNumbers("000123456789", "021000021"),
    )
    assert isinstance(provider, TransferProvider)

    auth = provider.authorize(_intent())
    assert auth.idempotency_key == "h1-2026-03-02-debit-submit"
    ref = provider.submit(auth)
    assert ref.provider_transfer_id == "ach_transfer_123"
    assert created["amount_cents"] == -5000  # the debit is negative
    assert created["idempotency_key"] == "h1-2026-03-02-debit-submit"
    assert provider.status(ref) == "settled"  # submitted + transaction, derived
    assert provider.handle_return(ref) is None


def test_the_increase_adapter_surfaces_a_return_code() -> None:
    class _Returned:
        def create_ach_transfer(self, **kwargs):  # noqa: ANN003, ANN201
            return "ach_x"

        def get_transfer(self, transfer_id):  # noqa: ANN001, ANN201
            return IncreaseTransferView(
                status="returned", transaction_exists=True, return_code="R01"
            )

    provider = IncreaseProvider(
        client=_Returned(),
        funding_account_id="acct_platform",
        numbers_for=lambda _h: AuthNumbers("1", "2"),
    )
    ret = provider.handle_return(provider.submit(provider.authorize(_intent())))
    assert ret is not None
    assert ret.return_code == "R01"
    assert ret.state == "returned"


# --- Method: the payoff leg --------------------------------------------------------------------


def test_a_payoff_may_only_target_a_liability() -> None:
    validate_destination(MethodDestination("mth_acc_1", "credit_card"))  # ok
    with pytest.raises(NotAPayableDestination, match="not a liability"):
        validate_destination(MethodDestination("mth_acc_2", "depository"))


def test_the_method_description_is_capped_at_ten_characters() -> None:
    assert description("RESFI") == "RESFI"
    assert len(description("A much longer descriptor")) == 10


def test_method_status_maps_posted_to_settled_and_reversals_to_returned() -> None:
    assert method_map_status("posted") == "settled"
    assert method_map_status("processing") == "pending"
    assert method_map_status("reversal_processing") == "returned"
    assert method_map_status("reversed") == "returned"
    assert method_map_status("failed") == "failed"
    # An unknown status is never a silent settled.
    assert method_map_status("who_knows") == "pending"


def test_method_amount_is_exact_cents() -> None:
    assert amount_cents(Decimal("50.00")) == 5000
    assert amount_cents(Decimal("1234.56")) == 123456


def test_the_method_adapter_submits_to_a_liability_and_reads_status() -> None:
    created: dict[str, object] = {}

    class _FakeMethod:
        def create_payment(self, **kwargs):  # noqa: ANN003, ANN201
            created.update(kwargs)
            return "pmt_123"

        def get_payment(self, payment_id):  # noqa: ANN001, ANN201
            return MethodPaymentView(status="posted", error_code=None)

    provider = MethodProvider(
        client=_FakeMethod(),
        source_id="mth_source_platform",
        destination_for=lambda _card: MethodDestination("mth_card_1", "credit_card"),
    )
    assert isinstance(provider, TransferProvider)

    ref = provider.submit(provider.authorize(_intent(leg="payoff", provider="method")))
    assert ref.provider_transfer_id == "pmt_123"
    assert created["source_id"] == "mth_source_platform"
    assert created["destination_id"] == "mth_card_1"
    assert created["amount_cents"] == 5000
    assert len(created["description"]) <= 10  # type: ignore[arg-type]
    assert provider.status(ref) == "settled"


def test_the_method_adapter_refuses_a_non_liability_destination() -> None:
    class _FakeMethod:
        def create_payment(self, **kwargs):  # noqa: ANN003, ANN201
            raise AssertionError("submit should refuse before creating a payment")

        def get_payment(self, payment_id):  # noqa: ANN001, ANN201
            ...

    provider = MethodProvider(
        client=_FakeMethod(),
        source_id="mth_source_platform",
        destination_for=lambda _card: MethodDestination("mth_chk", "depository"),
    )
    with pytest.raises(NotAPayableDestination):
        provider.submit(provider.authorize(_intent(leg="payoff", provider="method")))
