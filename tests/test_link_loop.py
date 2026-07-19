"""The Link loop, end to end against a real Postgres with a faked Plaid network.

The pieces the deferred Link rung was missing, proven together as the web page drives them:

    POST /households      → create a real household, become its owner
    POST /plaid/link/token → mint a link_token (the thing the client SDK opens)
    POST /plaid/link/exchange → public_token → a stored Item on that household
    POST /plaid/sync/now  → owner-triggered cursored sync → rows in plaid_transactions

The Plaid client is faked (no token or network leaves the test); household resolution, RLS scoping,
the append, and the cursor bookkeeping are the real code (as in `test_sync.py`). The verifier is
stubbed so no real Stytch project is needed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.identity import stytch
from backend.identity.deps import get_verifier
from backend.plaid.deps import get_plaid_client
from tests.conftest import requires_db

pytestmark = requires_db

TOKEN = "loop-session-token"
STYTCH_ID = "stytch-loop"


# --- a fake Plaid client covering every call the loop makes ------------------------------------


class _LinkToken:
    link_token = "link-sandbox-tok"
    expiration = "2026-07-18T12:00:00Z"


class _Exchange:
    access_token = "access-sandbox-tok"
    item_id = "item-loop"


class _Txn:
    def __init__(self, **d) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class _Page:
    added = [
        _Txn(
            transaction_id="t-1",
            account_id="acct-1",
            amount=12.34,
            date="2026-03-01",
            name="Coffee",
        )
    ]
    modified: list = []
    removed: list = []
    next_cursor = "c-end"
    has_more = False


class _Resp:
    """A Plaid response object: the ingest reads it through `.to_dict()`."""

    def __init__(self, d: dict) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class FakeLoopClient:
    def link_token_create(self, request):
        return _LinkToken()

    def item_public_token_exchange(self, request):
        return _Exchange()

    def transactions_sync(self, request):
        # One page, one added txn, on the first (cursor=None) call; empty thereafter.
        return _Page() if request.to_dict().get("cursor") is None else _EmptyPage()

    def accounts_balance_get(self, request):
        # A checking account (the funding account) and a card — what the account-state ingest the
        # sync now also drives must land for a household to be decidable.
        return _Resp(
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
                    },
                    {
                        "account_id": "acct-card",
                        "name": "Card",
                        "official_name": None,
                        "type": "credit",
                        "subtype": "credit card",
                        "balances": {
                            "current": 500.0,
                            "available": None,
                            "iso_currency_code": "USD",
                        },
                    },
                ]
            }
        )

    def liabilities_get(self, request):
        return _Resp(
            {
                "liabilities": {
                    "credit": [
                        {
                            "account_id": "acct-card",
                            "last_statement_balance": 500.0,
                            "last_statement_issue_date": "2026-03-01",
                            "minimum_payment_amount": 25.0,
                            "next_payment_due_date": "2026-03-20",
                            "aprs": [{"apr_type": "purchase_apr", "apr_percentage": 23.99}],
                            "is_overdue": False,
                        }
                    ]
                }
            }
        )


class _EmptyPage:
    added: list = []
    modified: list = []
    removed: list = []
    next_cursor = "c-end"
    has_more = False


def _verifier():
    def verify(token: str):
        if token == TOKEN:
            return STYTCH_ID, {}
        raise stytch.StytchVerificationError("stub: unknown token")

    return verify


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, app_engine: Engine) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", app_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def client(db) -> Iterator[TestClient]:
    from backend.main import app

    app.dependency_overrides[get_plaid_client] = lambda: FakeLoopClient()
    app.dependency_overrides[get_verifier] = _verifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_link_token_requires_a_session(client: TestClient) -> None:
    assert client.post("/plaid/link/token").status_code == 401


def test_link_token_is_minted_for_a_signed_in_user(client: TestClient) -> None:
    resp = client.post("/plaid/link/token", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["link_token"] == "link-sandbox-tok"


def test_the_whole_loop_lands_a_transaction(client: TestClient, db) -> None:
    # 1 — create the household (also JIT-provisions the user on first verified session).
    created = client.post("/households", headers=_auth())
    assert created.status_code == 201
    hid = created.json()["household_id"]

    # 2 — mint a link_token (the client SDK would open it).
    assert client.post("/plaid/link/token", headers=_auth()).status_code == 200

    # 3 — exchange the public_token the SDK returns → a stored Item on the caller's household.
    exchanged = client.post(
        "/plaid/link/exchange", headers=_auth(), json={"public_token": "public-x"}
    )
    assert exchanged.status_code == 200
    assert exchanged.json()["household_id"] == hid

    # 4 — owner-triggered sync pulls the transactions AND refreshes balances + card terms. The
    # account-state half is what makes the linked household decidable; without it `/live-decision`
    # is unreachable through the web flow (it drove only `/transactions/sync`).
    synced = client.post("/plaid/sync/now", headers=_auth())
    assert synced.status_code == 200
    body = synced.json()
    assert body["household_id"] == hid
    assert body["items_synced"] == 1
    assert body["added"] == 1
    assert body["accounts"] == 2, "the sync must also refresh balances (checking + card)"
    assert body["liabilities"] == 1, "and the card terms"

    # The transaction row actually landed, scoped to this household.
    with db.begin():
        rows = db.execute(
            text(
                "SELECT plaid_transaction_id, amount, name FROM plaid_transactions"
                " WHERE household_id = :h"
            ),
            {"h": hid},
        ).all()
    assert len(rows) == 1
    assert rows[0].plaid_transaction_id == "t-1"

    # And so did the account state — the rows `build_linked_history` reads.
    with db.begin():
        accts = db.execute(
            text("SELECT type, subtype FROM plaid_accounts WHERE household_id = :h"),
            {"h": hid},
        ).all()
        liabs = db.execute(
            text("SELECT count(*) FROM plaid_liabilities WHERE household_id = :h"),
            {"h": hid},
        ).scalar()
    assert {r.type for r in accts} == {"depository", "credit"}
    assert liabs == 1


def test_sync_now_requires_a_session(client: TestClient) -> None:
    assert client.post("/plaid/sync/now").status_code == 401
