"""Account-state ingest — the balances and card terms `/transactions/sync` cannot carry.

The Link rung, Phase 2. `plaid_transactions` records what a household *did*; this records what is
*true now* — the checking/savings balance that becomes `opening_balance`, and the card statement /
due date / APR that become a `CardSpec`. A transaction history has none of that, so without this a
linked household has nothing for `assemble_snapshot` to decide about.

`ingest_account_state(engine, plaid_item_id, client)` resolves the household (the `SECURITY DEFINER`
lookup, as `sync.py`), then under scope calls Plaid's `/accounts/balance/get` and `/liabilities/get`
and **appends** a snapshot row per account (migration 0014 — SELECT/INSERT only, latest-by-`seq`).

**Balances always; liabilities when the product is present.** `/accounts/balance/get` works on any
Item, so balances land for a household linked with only `transactions`. `/liabilities/get`
needs the `liabilities` product consented at link time; when it is absent Plaid raises, and this
degrades to "balances only" rather than failing the refresh — the card terms simply arrive the day
the Item is linked with liabilities. Amounts cross in as `Decimal(str(x))` (never `Decimal(float)`);
Plaid's APR **percentage** is converted to the engine's **fraction** (23.99 → 0.2399).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import plaid
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.db.repository import Repository
from backend.db.session import household_scope

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    status: str  # "ok" | "unknown_item"
    accounts: int = 0
    liabilities: int = 0


def _dec(value: Any) -> Decimal | None:
    """Plaid's float → `Decimal` through `str`, keeping the cent (ADR-0002 [2.2]). None passes."""
    return None if value is None else Decimal(str(value))


def _to_date(value: Any) -> date | None:
    """A Plaid date, whether it arrived as a `date` or an ISO string."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _purchase_apr(aprs: list[dict[str, Any]]) -> Decimal | None:
    """The purchase APR as a **fraction** (0.2399), or None if the issuer reports none.

    Plaid returns a list of APRs by type (purchase, cash, balance-transfer). The engine acts on the
    *purchase* rate — the one a revolving balance accrues at. None is a real answer, not a zero: it
    is exactly the `apr_source='estimated'` case ticket 0028 exists to keep honest, and a zero here
    would read as a 0% card.
    """
    for apr in aprs:
        if apr.get("apr_type") == "purchase_apr" and apr.get("apr_percentage") is not None:
            return Decimal(str(apr["apr_percentage"])) / Decimal("100")
    return None


def _resolve_household(engine: Engine, plaid_item_id: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT plaid_household_for_item(:item)"), {"item": plaid_item_id}
        ).scalar()


def ingest_account_state(engine: Engine, plaid_item_id: str, client: Any) -> IngestResult:
    """Fetch and store the current balances and card terms for one linked Item.

    Returns `unknown_item` if the Item resolves to no household (the same fail-soft as `run_sync`).
    Balances are stored first and always; liabilities are attempted and skipped cleanly if the Item
    has no `liabilities` product.
    """
    household_id = _resolve_household(engine, plaid_item_id)
    if household_id is None:
        return IngestResult("unknown_item")

    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        access_token = scoped.execute(
            text("SELECT access_token FROM plaid_items WHERE plaid_item_id = :item"),
            {"item": plaid_item_id},
        ).scalar_one()
        repo = Repository(conn=scoped, household_id=household_id)

        accounts = _ingest_balances(repo, plaid_item_id, access_token, client)
        liabilities = _ingest_liabilities(repo, plaid_item_id, access_token, client)

    return IngestResult("ok", accounts=accounts, liabilities=liabilities)


def _ingest_balances(repo: Repository, plaid_item_id: str, access_token: str, client: Any) -> int:
    from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

    response = client.accounts_balance_get(AccountsBalanceGetRequest(access_token=access_token))
    count = 0
    for account in response.to_dict().get("accounts", []):
        balances = account.get("balances") or {}
        repo.add_plaid_account(
            plaid_item_id=plaid_item_id,
            plaid_account_id=account["account_id"],
            name=account.get("name"),
            official_name=account.get("official_name"),
            type=_enum(account.get("type")),
            subtype=_enum(account.get("subtype")),
            current_balance=_dec(balances.get("current")),
            available_balance=_dec(balances.get("available")),
            iso_currency_code=balances.get("iso_currency_code"),
        )
        count += 1
    return count


def _ingest_liabilities(
    repo: Repository, plaid_item_id: str, access_token: str, client: Any
) -> int:
    from plaid.model.liabilities_get_request import LiabilitiesGetRequest

    try:
        response = client.liabilities_get(LiabilitiesGetRequest(access_token=access_token))
    except plaid.ApiException as exc:
        # The Item was linked without the `liabilities` product (or the issuer reports none). Not a
        # failure — balances still landed; the card terms arrive when liabilities is consented.
        log.info("liabilities unavailable for %s, storing balances only: %s", plaid_item_id, exc)
        return 0

    credit = (response.to_dict().get("liabilities") or {}).get("credit") or []
    count = 0
    for card in credit:
        repo.add_plaid_liability(
            plaid_item_id=plaid_item_id,
            plaid_account_id=card["account_id"],
            last_statement_balance=_dec(card.get("last_statement_balance")),
            last_statement_issue_date=_to_date(card.get("last_statement_issue_date")),
            minimum_payment=_dec(card.get("minimum_payment_amount")),
            next_payment_due_date=_to_date(card.get("next_payment_due_date")),
            purchase_apr=_purchase_apr(card.get("aprs") or []),
            is_overdue=card.get("is_overdue"),
        )
        count += 1
    return count


def _enum(value: Any) -> str | None:
    """A Plaid enum (`AccountType`/`AccountSubtype`) as its string value; str/None pass through."""
    if value is None:
        return None
    return str(value.value) if hasattr(value, "value") else str(value)
