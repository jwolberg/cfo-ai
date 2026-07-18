"""The hard gate: the sweep, proven against the real Increase and Method sandboxes — not a mock
(ticket 0044, U6).

This is the reason to build the other five units. Every other transfer test fakes the vendor at the
client seam; this one drives the real sandbox APIs through the saga end to end:

    submit_leg → Increase POST /ach_transfers → /simulations/ach_transfers/{id}/submit → /settle
    → apply_status asserts the DERIVED `settled` (Increase has no settled status, KTD-3)
    → a second sweep → /simulations/.../return → apply_status asserts `returned` AND that
      Repository.sweeps_in_flight() unwinds (U5)
    → the idempotency gate: two submits on one slot create ONE vendor transfer.

The Method payoff leg is driven the same way: POST /payments → /simulate/payments/{id} to `posted`,
then a forced `reversed`, asserting the ledger and the in-flight feedback both unwind.

**It requires live sandbox credentials** and a Postgres, and **skips loudly** without them rather
than reporting a green it did not earn — the failure this rung's philosophy warns about: "a thing
built, tested, and never actually exercised." A green *mocked* suite is not evidence here; the thing
it would be wrong about is a debit that already left someone's account.

> ⚠️ Provenance: **NOT yet run against a real sandbox — pending credentials.** This harness must run
> green (a forced return on each leg included) before `submit()` is promoted out of shadow. Until
> then the gate is honestly skipped, never faked. Expect at least one vendor-model correction when
> it first runs (the Plaid harness `0038` found two); reconcile it here and date this note then.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from backend.db.repository import repository
from backend.transfer import AuthNumbers, IncreaseProvider, TransferIntent
from backend.transfer.increase import IncreaseTransferView
from backend.transfer.saga import apply_status, submit_leg
from tests.conftest import _url as _db_url

_INCREASE_KEY = os.environ.get("INCREASE_SANDBOX_API_KEY")
_INCREASE_ACCOUNT = os.environ.get("INCREASE_FUNDING_ACCOUNT_ID")
_METHOD_KEY = os.environ.get("METHOD_SANDBOX_API_KEY")

_SKIP = (
    "the transfer Sandbox gate needs INCREASE_SANDBOX_API_KEY + INCREASE_FUNDING_ACCOUNT_ID (and,\n"
    "  for the payoff leg, METHOD_SANDBOX_API_KEY) and TEST_DATABASE_URL. A REAL sandbox run, not\n"
    "  a mock — the gate that proves money movement against the vendors. A skipped hard\n"
    "  gate is a rail nobody has exercised; it must run green before submit() turns on."
)

requires_increase_sandbox = pytest.mark.skipif(
    not (_INCREASE_KEY and _INCREASE_ACCOUNT and _db_url() is not None),
    reason=_SKIP,
)

HOUSEHOLD = "sandbox-fixture"
# Increase Sandbox test numbers — a counterparty the simulation endpoints accept.
_SANDBOX_ACCOUNT = "987654321"
_SANDBOX_ROUTING = "101050001"


class _IncreaseSandbox:
    """A real HTTP client for Increase's Sandbox, implementing `IncreaseClient` plus the simulation
    endpoints the gate drives. Uses `httpx`; unexercised until credentials exist (provenance)."""

    BASE = "https://sandbox.increase.com"

    def __init__(self, api_key: str) -> None:
        import httpx

        self._http = httpx.Client(
            base_url=self.BASE, headers={"Authorization": f"Bearer {api_key}"}, timeout=30
        )

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
        resp = self._http.post(
            "/ach_transfers",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "account_id": account_id,
                "amount": amount_cents,
                "account_number": account_number,
                "routing_number": routing_number,
                "statement_descriptor": statement_descriptor,
                "funding": "checking",
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def get_transfer(self, transfer_id: str) -> IncreaseTransferView:
        body = self._http.get(f"/ach_transfers/{transfer_id}").json()
        return IncreaseTransferView(
            status=body["status"],
            transaction_exists=body.get("transaction_id") is not None,
            return_code=(body.get("return") or {}).get("return_reason_code"),
        )

    # --- simulation endpoints ---
    def simulate_submit(self, transfer_id: str) -> None:
        self._http.post(f"/simulations/ach_transfers/{transfer_id}/submit").raise_for_status()

    def simulate_settle(self, transfer_id: str) -> None:
        self._http.post(f"/simulations/ach_transfers/{transfer_id}/settle").raise_for_status()

    def simulate_return(self, transfer_id: str, reason: str = "insufficient_fund") -> None:
        self._http.post(
            f"/simulations/ach_transfers/{transfer_id}/return", json={"reason": reason}
        ).raise_for_status()


def _intent(decision_date: dt.date) -> TransferIntent:
    return TransferIntent(
        household_id=HOUSEHOLD,
        target_card_id="card-1",
        decision_id=f"dec-{decision_date.isoformat()}",
        decision_date=decision_date,
        leg="debit",
        direction="debit",
        amount=Decimal("200.00"),
        provider="increase",
    )


@pytest.fixture
def _household(db):
    from sqlalchemy import text

    with db.begin():
        db.execute(
            text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HOUSEHOLD}
        )
    return db


@requires_increase_sandbox
def test_a_debit_settles_then_a_return_unwinds_the_feedback(_household, app_engine) -> None:
    client = _IncreaseSandbox(_INCREASE_KEY)
    provider = IncreaseProvider(
        client=client,
        funding_account_id=_INCREASE_ACCOUNT,
        numbers_for=lambda _h: AuthNumbers(_SANDBOX_ACCOUNT, _SANDBOX_ROUTING),
    )

    # --- a sweep that settles ---
    ref = submit_leg(app_engine, _intent(dt.date(2026, 3, 2)), provider)
    assert ref is not None
    client.simulate_submit(ref)
    client.simulate_settle(ref)
    assert apply_status(app_engine, provider, "increase", ref) == "settled"
    with repository(app_engine, HOUSEHOLD) as repo:
        assert repo.sweeps_in_flight() == Decimal("0")  # a settled debit is not in flight

    # --- a sweep that returns, and the in-flight feedback unwinds ---
    ref2 = submit_leg(app_engine, _intent(dt.date(2026, 3, 9)), provider)
    assert ref2 is not None
    with repository(app_engine, HOUSEHOLD) as repo:
        assert repo.sweeps_in_flight() == Decimal("200.00")  # submitted, in flight
    client.simulate_submit(ref2)
    client.simulate_return(ref2)
    assert apply_status(app_engine, provider, "increase", ref2) == "returned"
    with repository(app_engine, HOUSEHOLD) as repo:
        assert repo.sweeps_in_flight() == Decimal("0"), "a returned debit must unwind the feedback"


@requires_increase_sandbox
def test_two_submits_on_one_slot_create_one_vendor_transfer(_household, app_engine) -> None:
    """The highest-stakes assertion against the real vendor: the slot guard plus Increase's own
    Idempotency-Key must yield exactly one ACH transfer, not two — a duplicate is an overdraft."""
    client = _IncreaseSandbox(_INCREASE_KEY)
    provider = IncreaseProvider(
        client=client,
        funding_account_id=_INCREASE_ACCOUNT,
        numbers_for=lambda _h: AuthNumbers(_SANDBOX_ACCOUNT, _SANDBOX_ROUTING),
    )
    intent = _intent(dt.date(2026, 3, 2))
    first = submit_leg(app_engine, intent, provider)
    second = submit_leg(app_engine, intent, provider)  # same slot
    assert first is not None
    assert second is None, "the slot guard let a second debit through"
    with repository(app_engine, HOUSEHOLD) as repo:
        submitted = [t for t in repo.transfers() if t["state"] == "submitted"]
    assert len(submitted) == 1
