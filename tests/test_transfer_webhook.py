"""Provider-webhook verification and the unscoped raw store (ticket 0042, U4).

A forged or replayed callback must not fake a settlement or a return (KTD-5). The crypto is pure and
needs no database; the dedup persist and the SECURITY DEFINER lookup need a real Postgres.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from sqlalchemy import text

from backend.transfer.webhook import (
    TransferWebhookError,
    persist_webhook,
    verify_increase,
    verify_method,
)
from tests.conftest import requires_db

_SECRET = "whsec_test"
_BODY = b'{"event":"transfer.updated","id":"inc-1"}'


def _increase_headers(
    body: bytes, *, ts: int | None = None, secret: str = _SECRET
) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    signed = f"wh_1.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode()
    return {"webhook-id": "wh_1", "webhook-timestamp": str(ts), "webhook-signature": f"v1,{sig}"}


def _method_headers(body: bytes, *, ts: int | None = None, secret: str = _SECRET) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    signed = f"{ts}:".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return {"method-webhook-timestamp": str(ts), "method-webhook-signature": sig}


# --- Increase ----------------------------------------------------------------------------------


def test_a_valid_increase_signature_verifies() -> None:
    verify_increase(_increase_headers(_BODY), _BODY, _SECRET)  # does not raise


def test_a_tampered_increase_body_is_rejected() -> None:
    headers = _increase_headers(_BODY)
    with pytest.raises(TransferWebhookError, match="did not verify"):
        verify_increase(headers, _BODY + b"tampered", _SECRET)


def test_a_stale_increase_timestamp_is_rejected() -> None:
    old = int(time.time()) - 3600
    with pytest.raises(TransferWebhookError, match="stale"):
        verify_increase(_increase_headers(_BODY, ts=old), _BODY, _SECRET)


def test_a_wrong_increase_secret_is_rejected() -> None:
    with pytest.raises(TransferWebhookError, match="did not verify"):
        verify_increase(_increase_headers(_BODY, secret="wrong"), _BODY, _SECRET)


def test_missing_increase_headers_are_rejected() -> None:
    with pytest.raises(TransferWebhookError, match="missing"):
        verify_increase({}, _BODY, _SECRET)


# --- Method ------------------------------------------------------------------------------------


def test_a_valid_method_signature_verifies() -> None:
    verify_method(_method_headers(_BODY), _BODY, _SECRET)  # does not raise


def test_a_tampered_method_body_is_rejected() -> None:
    with pytest.raises(TransferWebhookError, match="did not verify"):
        verify_method(_method_headers(_BODY), _BODY + b"x", _SECRET)


def test_a_stale_method_timestamp_is_rejected() -> None:
    with pytest.raises(TransferWebhookError, match="stale"):
        verify_method(_method_headers(_BODY, ts=int(time.time()) - 3600), _BODY, _SECRET)


# --- Persistence and the definer lookup --------------------------------------------------------


@requires_db
def test_a_redelivered_webhook_dedups(db, db_engine) -> None:
    first = persist_webhook(
        db_engine,
        provider="increase",
        provider_transfer_id="inc-1",
        event_type="transfer.updated",
        event_id="evt-1",
        payload={"a": 1},
    )
    second = persist_webhook(
        db_engine,
        provider="increase",
        provider_transfer_id="inc-1",
        event_type="transfer.updated",
        event_id="evt-1",
        payload={"a": 1},
    )
    assert first is True
    assert second is False, "the redelivery was admitted — dedup failed"
    with db.begin():
        db.execute(text("DELETE FROM transfer_webhooks"))


@requires_db
def test_a_null_event_id_still_dedups(db, db_engine) -> None:
    """NULLS NOT DISTINCT: two events with no id collapse rather than both admit (KTD-5)."""
    a = persist_webhook(
        db_engine,
        provider="method",
        provider_transfer_id="pmt-1",
        event_type="payment.update",
        event_id=None,
        payload={},
    )
    b = persist_webhook(
        db_engine,
        provider="method",
        provider_transfer_id="pmt-1",
        event_type="payment.update",
        event_id=None,
        payload={},
    )
    assert a is True
    assert b is False
    with db.begin():
        db.execute(text("DELETE FROM transfer_webhooks"))


@requires_db
def test_the_definer_resolves_household_without_a_scope(db, app_engine) -> None:
    """The worker maps (provider, ref) → household before it can scope. An unscoped app session sees
    nothing in FORCE'd `transfers`, but the definer resolves it anyway — and returns only the id."""
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES ('alice', 'test')"))
        db.execute(
            text(
                "INSERT INTO transfers (id, household_id, target_card_id, decision_id,"
                " decision_date, leg, state, direction, amount, provider, provider_transfer_id,"
                " idempotency_key) VALUES ('xfer-a', 'alice', 'card-a', 'dec-a', '2026-03-02',"
                " 'debit', 'submitted', 'debit', '50.00', 'increase', 'inc-1', 'k1')"
            )
        )

    with app_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM transfers")).scalar() == 0
        )  # FORCE'd, no scope
        resolved = conn.execute(
            text("SELECT transfer_household_for_provider_ref('increase', 'inc-1')")
        ).scalar()
    assert resolved == "alice"
