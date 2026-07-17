"""The hard gate: the transport, proven against Plaid Sandbox itself — not a mock.

This is the reason to build the other four units. Every other test in this rung fakes the Plaid
client at the seam; this one drives the real Sandbox API end to end:

    /sandbox/public_token/create → exchange → sync → /sandbox/item/fire_webhook → sync again
    → assert the redelivery changed nothing → /sandbox/item/reset_login → assert `status` becomes
    login_required and the loop stops.

Because `/sandbox/public_token/create` bypasses Link entirely, this needs no UI — which is what lets
the Link flow leave this rung without leaving it unproven.

**It requires live Sandbox credentials** (`PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV=sandbox`)
and a Postgres. Without them it **skips loudly** rather than reporting a green it did not earn — the
same posture `tests/conftest.py` takes with the database suites, and the exact failure this rung's
own philosophy warns about: "a mechanism that was built, tested, and never actually exercised."

> ✅ Provenance: **run green against real Plaid Sandbox on 2026-07-17.** Two things Sandbox does
> differently from the plan were reconciled in the process, both now reflected here:
>   1. `/sandbox/item/fire_webhook` refuses (`SANDBOX_WEBHOOK_INVALID`) unless the item has a
>      webhook URL configured — so `_create_and_exchange` sets a placeholder one.
>   2. `fire_webhook SYNC_UPDATES_AVAILABLE` does **not** redeliver a notification; it *generates* a
>      new batch of transactions. So the redelivery-is-a-no-op proof is an unchanged re-sync (step
>      2), and the fired webhook is the *incremental*-update path (step 3). The plan modeled it as a
>      redelivery; Plaid's own behavior corrected that.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import repository
from backend.db.session import plaid_env
from backend.plaid.sync import run_sync
from tests.conftest import _url as _db_url

_HAVE_CREDENTIALS = bool(os.environ.get("PLAID_CLIENT_ID") and os.environ.get("PLAID_SECRET"))
_SKIP = (
    "the Plaid Sandbox gate needs PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=sandbox, and Postgres.\n"
    "  This is a REAL Sandbox run, not a mock — the one test that proves the transport against\n"
    "  Plaid itself. Set the credentials (Sandbox is free, auto-approved) and TEST_DATABASE_URL,\n"
    "  then run it. A skipped hard gate is a transport nobody has actually exercised."
)

requires_plaid_sandbox = pytest.mark.skipif(
    not (_HAVE_CREDENTIALS and plaid_env() == "sandbox" and _db_url() is not None),
    reason=_SKIP,
)

INSTITUTION = "ins_109508"  # Plaid Sandbox's "First Platypus Bank"
HOUSEHOLD = "sandbox-fixture"


def _make_client():
    from backend.plaid.client import make_plaid_client

    return make_plaid_client()


def _create_and_exchange(client) -> tuple[str, str]:
    """A Sandbox public token, exchanged for an access token and item id. No Link UI.

    A webhook URL is configured on the item — not because this test receives the webhook (it drives
    `run_sync` directly), but because `/sandbox/item/fire_webhook` refuses with
    `SANDBOX_WEBHOOK_INVALID` unless the item has one set. The URL is a placeholder; delivery is
    irrelevant to the redelivery-is-a-no-op assertion, which reads the stored cursor.
    """
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.products import Products
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
    from plaid.model.sandbox_public_token_create_request_options import (
        SandboxPublicTokenCreateRequestOptions,
    )

    public_token = client.sandbox_public_token_create(
        SandboxPublicTokenCreateRequest(
            institution_id=INSTITUTION,
            initial_products=[Products("transactions")],
            options=SandboxPublicTokenCreateRequestOptions(
                webhook="https://example.com/plaid/webhook"
            ),
        )
    ).public_token
    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    return exchange.access_token, exchange.item_id


def _fire_sync_webhook(client, access_token: str) -> None:
    from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest

    client.sandbox_item_fire_webhook(
        SandboxItemFireWebhookRequest(
            access_token=access_token, webhook_code="SYNC_UPDATES_AVAILABLE"
        )
    )


def _reset_login(client, access_token: str) -> None:
    from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest

    client.sandbox_item_reset_login(SandboxItemResetLoginRequest(access_token=access_token))


def _sync_until_rows(engine: Engine, item_id: str, client, *, attempts: int = 5):
    """First sync, retried: Sandbox may need a beat before an item's transactions are ready."""
    for _ in range(attempts):
        result = run_sync(engine, item_id, client)
        if result.added or result.modified or result.removed:
            return result
        time.sleep(2)
    return result


def _item(db, item_id: str):
    with db.begin():
        return db.execute(
            text(
                "SELECT cursor, status, last_successful_sync_at FROM plaid_items"
                " WHERE plaid_item_id = :i"
            ),
            {"i": item_id},
        ).one()


def _txn_rows(db, item_id: str) -> list:
    with db.begin():
        return db.execute(
            text("SELECT id FROM plaid_transactions WHERE plaid_item_id = :i"),
            {"i": item_id},
        ).all()


@requires_plaid_sandbox
def test_the_transport_works_end_to_end_against_plaid_sandbox(db, app_engine: Engine) -> None:
    client = _make_client()

    with db.begin():
        db.execute(
            text(
                "INSERT INTO households (id, archetype) VALUES (:h, 'test') ON CONFLICT DO NOTHING"
            ),
            {"h": HOUSEHOLD},
        )

    access_token, item_id = _create_and_exchange(client)
    with repository(app_engine, HOUSEHOLD) as repo:
        repo.add_plaid_item(
            item_id=f"pi-{item_id}",
            plaid_item_id=item_id,
            access_token=access_token,
            institution_id=INSTITUTION,
        )

    # 1. The first sync lands real rows and stamps freshness.
    first = _sync_until_rows(app_engine, item_id, client)
    assert first.status == "ok"
    assert first.added > 0, "Sandbox returned no transactions to ingest"
    item = _item(db, item_id)
    assert item.status == "healthy"
    assert item.cursor is not None
    assert item.last_successful_sync_at is not None

    # 2. An immediate re-sync from the stored cursor is a no-op. **This**, not a fired webhook, is
    #    the redelivery-is-a-no-op proof: nothing changed at Plaid, so the cursor is at the end and
    #    /transactions/sync returns an empty page. (Sandbox's fire_webhook does NOT redeliver — it
    #    *generates* new transactions, which is step 3.)
    second = run_sync(app_engine, item_id, client)
    assert (second.added, second.modified, second.removed) == (0, 0, 0), (
        "an unchanged re-sync was not a no-op — the cursor did not persist, or the loop re-fetched"
    )

    # 3. Fire SYNC_UPDATES_AVAILABLE. In Sandbox this makes Plaid generate a *new* batch; the next
    #    sync consumes it incrementally **from the stored cursor** (not from scratch — the row count
    #    would double if it re-fetched), and then converges back to a no-op. That whole cycle is the
    #    cursor mechanics the plan set out to prove against Plaid itself.
    before = len(_txn_rows(db, item_id))
    _fire_sync_webhook(client, access_token)
    third = run_sync(app_engine, item_id, client)
    assert third.status == "ok"
    after_update = len(_txn_rows(db, item_id))
    assert after_update == before + third.added + third.modified + third.removed, (
        "the incremental sync did not append exactly its delta — the cursor reset or double-counted"
    )
    converged = run_sync(app_engine, item_id, client)
    assert (converged.added, converged.modified, converged.removed) == (0, 0, 0)

    # Freshness after the last successful sync — it must NOT advance on the failure below.
    freshness_before_failure = _item(db, item_id).last_successful_sync_at

    # 4. Break the login and sync: status flips to login_required, the loop stops, freshness frozen.
    _reset_login(client, access_token)
    failed = run_sync(app_engine, item_id, client)
    assert failed.status == "login_required"
    after = _item(db, item_id)
    assert after.status == "login_required"
    assert after.last_successful_sync_at == freshness_before_failure, (
        "freshness advanced on a failed sync — balance_age_days would lie"
    )


@requires_plaid_sandbox
@pytest.mark.skip(
    reason="Forcing a `removed` in Sandbox is not deterministic from the sync flow alone — it "
    "needs a custom Sandbox user or the /sandbox/transactions endpoints. Wire the exact mechanism "
    "on first run with credentials; U3 already proves a Plaid-shaped removed inserts with NULL "
    "columns at the schema layer (tests/test_schema.py::TestPlaidTransactions)."
)
def test_a_removed_transaction_lands_as_a_null_column_row() -> None:  # pragma: no cover
    """The plan's sparse-payload case, end to end. Left as an explicit, un-green TODO rather than a
    fabricated pass — the honest state until a real Sandbox run pins down how to force a removal."""
