"""The cursored `/transactions/sync` loop: scoping, resumability, the login-required halt, and the
per-item lock that stops the two triggers racing into duplicate rows.

`run_sync` is driven against a **real Postgres** with a **fake Plaid client** — the network is
faked, but the household resolution, the RLS scoping, the append, and the cursor bookkeeping are the
real code. It runs as `cfo_app` (via `app_engine`), so RLS actually binds; the arranging is done as
the owner (`db`), the way production splits an admin task from the service.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from decimal import Decimal

import plaid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.plaid.deps import get_oidc_verifier, get_plaid_client
from backend.plaid.sync import refresh_item, run_sync
from tests.conftest import requires_db

pytestmark = requires_db


# --- a fake Plaid client that returns programmed pages keyed by the incoming cursor -------------


class _Txn:
    def __init__(self, **d) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class _Page:
    def __init__(self, *, added=(), modified=(), removed=(), next_cursor="c-end", has_more=False):
        self.added = list(added)
        self.modified = list(modified)
        self.removed = list(removed)
        self.next_cursor = next_cursor
        self.has_more = has_more


class FakeSyncClient:
    def __init__(self, pages: dict) -> None:
        self.pages = pages  # cursor (or None) -> _Page
        self.calls: list[str | None] = []

    def transactions_sync(self, request):
        cursor = request.to_dict().get("cursor")
        self.calls.append(cursor)
        return self.pages[cursor]


class LoginRequiredClient:
    def transactions_sync(self, request):
        exc = plaid.ApiException(status=400)
        exc.body = json.dumps({"error_code": "ITEM_LOGIN_REQUIRED", "error_type": "ITEM_ERROR"})
        raise exc


def _added(txn_id: str, account="acct-1", amount="12.34", name="Coffee") -> _Txn:
    return _Txn(
        transaction_id=txn_id,
        account_id=account,
        amount=float(amount),
        date="2026-03-01",
        name=name,
    )


def _removed(txn_id: str, account="acct-1") -> _Txn:
    return _Txn(transaction_id=txn_id, account_id=account)


def _arrange_item(db, household_id: str, plaid_item_id: str, cursor: str | None = None) -> None:
    with db.begin():
        db.execute(
            text(
                "INSERT INTO households (id, archetype) VALUES (:h, 'test') ON CONFLICT DO NOTHING"
            ),
            {"h": household_id},
        )
        db.execute(
            text(
                "INSERT INTO plaid_items (id, household_id, plaid_item_id, access_token, cursor)"
                " VALUES (:id, :h, :pid, 'access-sandbox', :cur)"
            ),
            {"id": f"pi-{plaid_item_id}", "h": household_id, "pid": plaid_item_id, "cur": cursor},
        )


def _txn_rows(db, plaid_item_id: str) -> list:
    with db.begin():
        return db.execute(
            text(
                "SELECT household_id, plaid_transaction_id, change_type, amount"
                " FROM plaid_transactions WHERE plaid_item_id = :i ORDER BY plaid_transaction_id"
            ),
            {"i": plaid_item_id},
        ).all()


def _item(db, plaid_item_id: str) -> object:
    with db.begin():
        return db.execute(
            text(
                "SELECT cursor, status, last_successful_sync_at FROM plaid_items"
                " WHERE plaid_item_id = :i"
            ),
            {"i": plaid_item_id},
        ).one()


# --- the loop -----------------------------------------------------------------------------------


class TestTheCursoredLoop:
    def test_a_first_sync_appends_every_outcome_and_stamps_freshness(
        self, db, app_engine: Engine
    ) -> None:
        _arrange_item(db, "alice", "item-a")
        client = FakeSyncClient(
            {
                None: _Page(
                    added=[_added("t1"), _added("t2")],
                    modified=[_added("t1")],  # a correction to t1, as a new row
                    removed=[_removed("t3")],
                    next_cursor="c-1",
                    has_more=False,
                )
            }
        )
        result = run_sync(app_engine, "item-a", client)

        assert (result.status, result.added, result.modified, result.removed) == ("ok", 2, 1, 1)
        rows = _txn_rows(db, "item-a")
        assert len(rows) == 4, "every added/modified/removed should be its own row"
        assert all(r.household_id == "alice" for r in rows)
        # the removed row carries a NULL amount; an added row round-trips its Decimal exactly
        removed = [r for r in rows if r.change_type == "removed"]
        assert len(removed) == 1 and removed[0].amount is None
        assert any(r.amount == Decimal("12.34") for r in rows)

        item = _item(db, "item-a")
        assert item.cursor == "c-1", "the cursor was persisted"
        assert item.status == "healthy"
        assert item.last_successful_sync_at is not None, "freshness was stamped"

    def test_it_pages_until_has_more_is_false(self, db, app_engine: Engine) -> None:
        _arrange_item(db, "alice", "item-a")
        client = FakeSyncClient(
            {
                None: _Page(added=[_added("t1")], next_cursor="c-1", has_more=True),
                "c-1": _Page(added=[_added("t2")], next_cursor="c-2", has_more=False),
            }
        )
        result = run_sync(app_engine, "item-a", client)
        assert result.added == 2
        assert client.calls == [None, "c-1"], "it did not page through with the returned cursor"
        assert _item(db, "item-a").cursor == "c-2"

    def test_a_resync_from_the_stored_cursor_is_a_no_op(self, db, app_engine: Engine) -> None:
        """Redelivery and resumability in one: a second run reads the advanced cursor and Plaid
        returns an empty page, so nothing new is written."""
        _arrange_item(db, "alice", "item-a", cursor="c-1")
        client = FakeSyncClient({"c-1": _Page(next_cursor="c-1", has_more=False)})
        result = run_sync(app_engine, "item-a", client)
        assert (result.added, result.modified, result.removed) == (0, 0, 0)
        assert _txn_rows(db, "item-a") == []
        assert client.calls == ["c-1"], "it must resume from the stored cursor, not from the start"

    def test_login_required_flips_status_and_leaves_freshness_untouched(
        self, db, app_engine: Engine
    ) -> None:
        _arrange_item(db, "alice", "item-a", cursor="c-0")
        result = run_sync(app_engine, "item-a", LoginRequiredClient())
        assert result.status == "login_required"
        item = _item(db, "item-a")
        assert item.status == "login_required"
        assert item.cursor == "c-0", "the cursor must not advance on failure"
        assert item.last_successful_sync_at is None, "freshness must not be stamped on failure"
        assert _txn_rows(db, "item-a") == [], "a failed sync must leave no partial rows"

    def test_a_sync_lands_only_in_the_items_own_household(self, db, app_engine: Engine) -> None:
        """Scoping: item-b resolves to bob, and its rows are bob's — alice's table stays empty."""
        _arrange_item(db, "alice", "item-a")
        _arrange_item(db, "bob", "item-b")
        client = FakeSyncClient({None: _Page(added=[_added("t1")], has_more=False)})
        run_sync(app_engine, "item-b", client)

        assert _txn_rows(db, "item-a") == []
        bob_rows = _txn_rows(db, "item-b")
        assert len(bob_rows) == 1 and bob_rows[0].household_id == "bob"

    def test_an_unknown_item_is_a_no_op(self, db, app_engine: Engine) -> None:
        client = FakeSyncClient({None: _Page(added=[_added("t1")])})
        result = run_sync(app_engine, "item-does-not-exist", client)
        assert result.status == "unknown_item"


class TestTheTwoTriggersDoNotRace:
    def test_a_concurrent_run_blocks_on_the_item_lock_and_does_not_double_insert(
        self, db, app_engine: Engine
    ) -> None:
        """The `SELECT ... FOR UPDATE` guard. A second run on the same item blocks until the first
        commits, then reads the advanced cursor and finds nothing — one page of rows, not two.
        """
        _arrange_item(db, "alice", "item-a")

        release = threading.Event()

        class _BlockingClient:
            """Serves the first page once (blocking until released), then empty from the cursor."""

            def __init__(self) -> None:
                self.first_done = False

            def transactions_sync(self, request):
                cursor = request.to_dict().get("cursor")
                if cursor is None:
                    release.wait(timeout=10)
                    return _Page(added=[_added("t1"), _added("t2")], next_cursor="c-1")
                return _Page(next_cursor="c-1", has_more=False)

        client = _BlockingClient()
        results: dict[str, object] = {}

        def _first() -> None:
            results["first"] = run_sync(app_engine, "item-a", client)

        def _second() -> None:
            results["second"] = run_sync(app_engine, "item-a", client)

        t1 = threading.Thread(target=_first)
        t1.start()
        # Give the first thread time to take the FOR UPDATE lock before the second starts.
        threading.Event().wait(0.5)
        t2 = threading.Thread(target=_second)
        t2.start()
        # The second thread is now blocked on the lock; let the first finish, which releases it.
        release.set()
        t1.join(timeout=15)
        t2.join(timeout=15)

        rows = _txn_rows(db, "item-a")
        assert len(rows) == 2, f"the two runs double-inserted: {len(rows)} rows"


class TestTheWorkerRouteIsGatedByOidc:
    """The Cloud Tasks push target must be reachable only with a valid OIDC token. The verifier is
    faked here (the real one calls Google); the point is that the route *consults* it and refuses
    without it, and that a permitted request actually runs the sync."""

    @pytest.fixture(autouse=True)
    def secrets(self, monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
        from backend.auth import API_KEY_ENV

        monkeypatch.setenv(API_KEY_ENV, "test-key-not-a-real-one")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))

    @pytest.fixture
    def client(self, db) -> Iterator[TestClient]:
        from backend.main import app

        fake = FakeSyncClient({None: _Page(added=[_added("t1")], has_more=False)})
        app.dependency_overrides[get_plaid_client] = lambda: fake
        app.dependency_overrides[get_oidc_verifier] = lambda: lambda auth: auth == "Bearer ok"
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    def test_a_request_without_a_token_is_refused(self, client: TestClient, db) -> None:
        _arrange_item(db, "alice", "item-a")
        response = client.post("/plaid/sync/worker", json={"plaid_item_id": "item-a"})
        assert response.status_code == 401
        assert _txn_rows(db, "item-a") == [], "the worker ran without a token"

    def test_a_permitted_request_runs_the_sync(self, client: TestClient, db) -> None:
        _arrange_item(db, "alice", "item-a")
        response = client.post(
            "/plaid/sync/worker",
            json={"plaid_item_id": "item-a"},
            headers={"Authorization": "Bearer ok"},
        )
        assert response.status_code == 200
        assert response.json()["added"] == 1
        assert len(_txn_rows(db, "item-a")) == 1


# --- refresh_item: transactions AND the account-state half, composed at the trigger boundary -----


class _AccountResp:
    def __init__(self, d: dict) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class RefreshClient(FakeSyncClient):
    """Transactions plus a working balance fetch. Liabilities is absent (the Item was linked with
    only `transactions`), so `/liabilities/get` raises — `ingest_account_state` degrades to
    balances-only, exactly as in production."""

    def accounts_balance_get(self, request):
        return _AccountResp(
            {
                "accounts": [
                    {
                        "account_id": "acct-1",
                        "name": "Checking",
                        "official_name": None,
                        "type": "depository",
                        "subtype": "checking",
                        "balances": {
                            "current": 1000.0,
                            "available": 950.0,
                            "iso_currency_code": "USD",
                        },
                    }
                ]
            }
        )

    def liabilities_get(self, request):
        exc = plaid.ApiException(status=400)
        exc.body = json.dumps(
            {"error_code": "PRODUCTS_NOT_SUPPORTED", "error_type": "INVALID_INPUT"}
        )
        raise exc


class BrokenBalanceClient(FakeSyncClient):
    """Transactions sync fine, but the balance fetch blows up — the fail-soft case."""

    def accounts_balance_get(self, request):
        raise RuntimeError("plaid balance blip")


class TestRefreshItem:
    def test_it_syncs_transactions_and_refreshes_balances(self, db, app_engine: Engine) -> None:
        _arrange_item(db, "alice", "item-a")
        client = RefreshClient({None: _Page(added=[_added("t1")], has_more=False)})
        result = refresh_item(app_engine, "item-a", client)
        assert (result.sync.status, result.sync.added) == ("ok", 1)
        assert (result.accounts.status, result.accounts.accounts) == ("ok", 1)
        assert result.accounts.liabilities == 0, "liabilities absent → balances-only, not a failure"
        assert len(_txn_rows(db, "item-a")) == 1

    def test_a_balance_failure_is_fail_soft_and_the_transactions_still_land(
        self, db, app_engine: Engine
    ) -> None:
        _arrange_item(db, "alice", "item-a")
        client = BrokenBalanceClient({None: _Page(added=[_added("t1")], has_more=False)})
        result = refresh_item(app_engine, "item-a", client)
        assert result.sync.added == 1, "a balance blip must not fail the transaction sync"
        assert result.accounts.status == "error"
        assert len(_txn_rows(db, "item-a")) == 1

    def test_a_failed_sync_skips_the_refresh(self, db, app_engine: Engine) -> None:
        _arrange_item(db, "alice", "item-a", cursor="c-0")
        result = refresh_item(app_engine, "item-a", LoginRequiredClient())
        assert result.sync.status == "login_required"
        assert result.accounts.status == "login_required", "nothing fresh to read on a failed sync"
