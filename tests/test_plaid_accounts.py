"""Account-state ingest (`backend/plaid/accounts.py`) — balances and card terms into 0014's tables.

Driven against a **real Postgres** with a **fake Plaid client**: the network is faked, the household
resolution, RLS scoping, the append, and the latest-by-`seq` read are the real code. The claims:

- a refresh stores one balance snapshot per account and one card-terms snapshot per credit card;
- Plaid's APR **percentage** lands as the engine's **fraction** (23.99 → 0.2399);
- the `liabilities` product being absent degrades to "balances only", never a failed refresh;
- a re-fetch supersedes the prior snapshot (latest-by-`seq`), without rewriting it;
- an unknown Item resolves to `unknown_item`, not a crash.
"""

from __future__ import annotations

from decimal import Decimal

import plaid
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import repository
from backend.plaid.accounts import ingest_account_state
from tests.conftest import requires_db

pytestmark = requires_db

HH = "hh_accts"
ITEM = "item-accts"


class _Resp:
    def __init__(self, d: dict) -> None:
        self._d = d

    def to_dict(self) -> dict:
        return self._d


def _balances(current: float, subtype: str = "checking") -> dict:
    return {
        "account_id": f"acct-{subtype}",
        "name": subtype.title(),
        "official_name": f"Big Bank {subtype}",
        "type": "depository" if subtype in ("checking", "savings") else "credit",
        "subtype": subtype,
        "balances": {"current": current, "available": current, "iso_currency_code": "USD"},
    }


class FakeAccountsClient:
    def __init__(self, *, liabilities: bool = True) -> None:
        self._liabilities = liabilities

    def accounts_balance_get(self, request):
        return _Resp(
            {
                "accounts": [
                    _balances(2400.55, "checking"),
                    _balances(9000.00, "savings"),
                    _balances(-1400.00, "credit card"),
                ]
            }
        )

    def liabilities_get(self, request):
        if not self._liabilities:
            raise plaid.ApiException(status=400)
        return _Resp(
            {
                "liabilities": {
                    "credit": [
                        {
                            "account_id": "acct-credit card",
                            "last_statement_balance": 1200.00,
                            "last_statement_issue_date": "2026-06-20",
                            "minimum_payment_amount": 35.00,
                            "next_payment_due_date": "2026-07-15",
                            "aprs": [
                                {"apr_type": "purchase_apr", "apr_percentage": 23.99},
                                {"apr_type": "cash_apr", "apr_percentage": 29.99},
                            ],
                            "is_overdue": False,
                        }
                    ]
                }
            }
        )


@pytest.fixture
def item(db):
    with db.begin():
        db.execute(text("INSERT INTO households (id, archetype) VALUES (:h, 'test')"), {"h": HH})
        db.execute(
            text(
                "INSERT INTO plaid_items (id, household_id, plaid_item_id, access_token)"
                " VALUES ('pi-accts', :h, :pid, 'access-sandbox')"
            ),
            {"h": HH, "pid": ITEM},
        )
    return db


def test_a_refresh_stores_balances_and_card_terms(item, app_engine: Engine) -> None:
    result = ingest_account_state(app_engine, ITEM, FakeAccountsClient())
    assert result.status == "ok"
    assert result.accounts == 3
    assert result.liabilities == 1

    with repository(app_engine, HH) as repo:
        accounts = {a["plaid_account_id"]: a for a in repo.latest_plaid_accounts()}
        liabilities = repo.latest_plaid_liabilities()

    assert accounts["acct-checking"]["current_balance"] == Decimal("2400.55")
    assert accounts["acct-checking"]["subtype"] == "checking"
    assert accounts["acct-credit card"]["type"] == "credit"

    assert len(liabilities) == 1
    card = liabilities[0]
    assert card["last_statement_balance"] == Decimal("1200.00")
    # The engine's fraction, not Plaid's percentage.
    assert card["purchase_apr"] == Decimal("0.2399")
    assert card["is_overdue"] is False


def test_liabilities_absent_degrades_to_balances_only(item, app_engine: Engine) -> None:
    result = ingest_account_state(app_engine, ITEM, FakeAccountsClient(liabilities=False))
    assert result.status == "ok"
    assert result.accounts == 3
    assert result.liabilities == 0  # the product wasn't there — balances still landed

    with repository(app_engine, HH) as repo:
        assert len(repo.latest_plaid_accounts()) == 3
        assert repo.latest_plaid_liabilities() == []


def test_a_refetch_supersedes_by_seq(item, app_engine: Engine) -> None:
    ingest_account_state(app_engine, ITEM, FakeAccountsClient())

    class Refetched(FakeAccountsClient):
        def accounts_balance_get(self, request):
            return _Resp({"accounts": [_balances(50.00, "checking")]})

    ingest_account_state(app_engine, ITEM, Refetched())

    with repository(app_engine, HH) as repo:
        accounts = {a["plaid_account_id"]: a for a in repo.latest_plaid_accounts()}
    # The latest reader returns the newer balance; the older row still exists underneath it.
    assert accounts["acct-checking"]["current_balance"] == Decimal("50.00")


def test_an_unknown_item_is_reported_not_crashed(app_engine: Engine) -> None:
    result = ingest_account_state(app_engine, "no-such-item", FakeAccountsClient())
    assert result.status == "unknown_item"
