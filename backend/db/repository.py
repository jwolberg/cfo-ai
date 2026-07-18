"""Household-scoped data access. The other half of `architecture.md` §7.1's authz pattern.

> Every table is scoped by `household_id`, enforced at the repository layer **and** by Postgres
> row-level security. An IDOR here exposes someone's complete financial life; one forgotten
> `WHERE` clause is not an acceptable single point of failure.

**Two layers, and they are independent on purpose.** RLS is the backstop; this is the fence. Either
alone would probably hold. "Probably" is not the bar for a table that holds a household's complete
financial life, and `tests/test_idor.py` proves each of them *with the other removed* — defense in
depth that is only ever tested end-to-end is one layer wearing a disguise.

**Scope is a constructor argument, never a method argument.** A per-call `household_id` is a thing
someone forgets exactly once. There is no way to hold a `Repository` and not know whose it is.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, text

from backend.db.session import household_scope


@dataclass(frozen=True)
class Repository:
    """Every query below is scoped to `household_id`. There is no unscoped path.

    Frozen: the scope cannot be reassigned after construction, so a repository handed to a
    function cannot be quietly repointed at another household.
    """

    conn: Connection
    household_id: str

    # --- reads -----------------------------------------------------------------------

    def accounts(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM accounts WHERE household_id = :h ORDER BY id")

    def cards(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM cards WHERE household_id = :h ORDER BY id")

    def policy(self) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM policies WHERE household_id = :h")
        return rows[0] if rows else None

    def decisions(self, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decisions WHERE household_id = :h"
        params: dict[str, Any] = {"h": self.household_id}
        if start is not None:
            sql += " AND day >= :start"
            params["start"] = start
        if end is not None:
            sql += " AND day <= :end"
            params["end"] = end
        sql += " ORDER BY day"
        return [dict(r) for r in self.conn.execute(text(sql), params).mappings()]

    def decision_on(self, day: date) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM decisions WHERE household_id = :h AND day = :d", d=day)
        return rows[0] if rows else None

    def last_decision(self) -> dict[str, Any] | None:
        """The most recent day on record — the household's "today".

        `/spend` renders one day, not a window, so it reads one row rather than pulling ninety and
        discarding eighty-nine. The demo's today is a fixed calendar date (the last day seeded),
        not the wall clock, which is why this is `MAX(day)` and not `CURRENT_DATE`.
        """
        rows = self._all(
            "SELECT * FROM decisions WHERE household_id = :h ORDER BY day DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def spend_projection(self) -> dict[str, Any] | None:
        """The History-derived half of the spend surface, or `None` if it was never seeded.

        `None` is a real answer and the caller must render it as one: it means this household has
        decisions but no projection — see `backend/spend.py` and ticket `0031`.
        """
        rows = self._all("SELECT * FROM spend_projections WHERE household_id = :h")
        return rows[0] if rows else None

    # --- writes ----------------------------------------------------------------------
    #
    # The seeder (ticket 0023) writes through exactly these, which is the point: it exercises the
    # same path a live daily job would, rather than a bulk-insert shortcut that would prove
    # nothing about the code that eventually moves money.

    def add_account(
        self,
        *,
        account_id: str,
        kind: str,
        balance: Decimal,
        connection: str,
        balance_age_days: int,
    ) -> None:
        self._exec(
            "INSERT INTO accounts (id, household_id, kind, balance, connection, balance_age_days)"
            " VALUES (:a, :h, :k, :b, :c, :age)",
            a=account_id,
            k=kind,
            b=balance,
            c=connection,
            age=balance_age_days,
        )

    def add_card(self, **f: Any) -> None:
        """Insert a card. `apr_source` is **required**, which is the whole point of naming it here.

        It was missing from this INSERT until ticket `0023`, and the omission was silent: the column
        carries `server_default 'reported'` so a live table could be backfilled without a rewrite
        (migration `0002`), which means an INSERT that simply never mentions it succeeds and records
        a **guess as a reported fact**. Writing a 23% estimate through here stored
        `apr=0.23000, apr_source=reported`, and the column's own comment says what that costs:
        "a 23% estimate and a reported 23% are the same number, and only this column tells them
        apart... losing this would silently start billing the KPI against a guess."

        That is ticket `0028` — *act on the estimate, never bill for it* — defeated by a column
        nobody wrote. Nothing caught it because nothing wrote a card through this repository at all
        until the seeder; `0021` built the method and `0028` added the column two PRs later.

        Naming the column in the statement is what makes it required: SQLAlchemy raises on a missing
        bind parameter, so forgetting it is now an error at the boundary rather than a household
        whose rate provenance quietly became a fact.
        """
        self._exec(
            "INSERT INTO cards (id, household_id, apr, apr_source, close_day_of_month, grace_days,"
            " statement_balance, statement_due_date, minimum_payment, unbilled_balance,"
            " next_close_date, behavior, observed_monthly_payment, observed_monthly_charges)"
            " VALUES (:card_id, :h, :apr, :apr_source, :close_day_of_month, :grace_days,"
            " :statement_balance, :statement_due_date, :minimum_payment, :unbilled_balance,"
            " :next_close_date, :behavior, :observed_monthly_payment, :observed_monthly_charges)",
            **f,
        )

    def set_policy(self, **f: Any) -> None:
        self._exec(
            "INSERT INTO policies (household_id, buffer_floor, max_sweep, max_weekly_sweep,"
            " min_days_between_sweeps, blackout_dates)"
            " VALUES (:h, :buffer_floor, :max_sweep, :max_weekly_sweep, :min_days_between_sweeps,"
            " :blackout_dates)"
            " ON CONFLICT (household_id) DO UPDATE SET"
            " buffer_floor = EXCLUDED.buffer_floor, max_sweep = EXCLUDED.max_sweep,"
            " max_weekly_sweep = EXCLUDED.max_weekly_sweep,"
            " min_days_between_sweeps = EXCLUDED.min_days_between_sweeps,"
            " blackout_dates = EXCLUDED.blackout_dates",
            **f,
        )

    def add_plaid_item(
        self,
        *,
        item_id: str,
        plaid_item_id: str,
        access_token: str,
        institution_id: str | None = None,
    ) -> None:
        """Insert a linked Plaid Item, scoped to this household (ticket 0034).

        Through the repository like every other write, so RLS `WITH CHECK` binds the row to
        `household_id`: a caller that named the wrong household cannot smuggle an item elsewhere,
        the INSERT is simply rejected. `access_token` is plaintext in Sandbox — the start guard
        (`backend/db/session.py`) forbids a non-sandbox boot until it is ciphertext.
        """
        self._exec(
            "INSERT INTO plaid_items"
            " (id, household_id, plaid_item_id, institution_id, access_token)"
            " VALUES (:item_id, :h, :plaid_item_id, :institution_id, :access_token)",
            item_id=item_id,
            plaid_item_id=plaid_item_id,
            institution_id=institution_id,
            access_token=access_token,
        )

    def plaid_item_ids(self) -> list[str]:
        """Every linked Item's Plaid id for this household. The nightly poll enumerates households
        (unscoped — `households` has no RLS, it is the tenant registry) and asks each one this."""
        rows = self._all("SELECT plaid_item_id FROM plaid_items WHERE household_id = :h")
        return [r["plaid_item_id"] for r in rows]

    def add_plaid_transaction(
        self,
        *,
        transaction_id: str,
        plaid_item_id: str,
        plaid_account_id: str,
        plaid_transaction_id: str,
        change_type: str,
        pending_transaction_id: str | None = None,
        amount: Decimal | None = None,
        date: date | None = None,
        name: str | None = None,
        merchant_name: str | None = None,
    ) -> None:
        """Append one `/transactions/sync` outcome (ticket 0036). INSERT only — the table grants the
        app role no UPDATE or DELETE, so a correction is a new row, never a rewrite.

        `amount` is a `Decimal`. The caller converts Plaid's float through `str` (never
        `Decimal(float)`), so the cent that reaches this NUMERIC column is the cent Plaid sent.
        """
        self._exec(
            "INSERT INTO plaid_transactions"
            " (id, household_id, plaid_item_id, plaid_account_id, plaid_transaction_id,"
            " pending_transaction_id, amount, date, name, merchant_name, change_type)"
            " VALUES (:tid, :h, :plaid_item_id, :plaid_account_id, :plaid_transaction_id,"
            " :pending_transaction_id, :amount, :date, :name, :merchant_name, :change_type)",
            tid=transaction_id,
            plaid_item_id=plaid_item_id,
            plaid_account_id=plaid_account_id,
            plaid_transaction_id=plaid_transaction_id,
            pending_transaction_id=pending_transaction_id,
            amount=amount,
            date=date,
            name=name,
            merchant_name=merchant_name,
            change_type=change_type,
        )

    def transfers(self) -> list[dict[str, Any]]:
        """Every transfer-ledger row for this household, oldest first (ticket 0039).

        The ledger is append-only, so a single logical transfer appears as several rows — one per
        state transition. The latest-state-per-slot and non-terminal-sum queries the saga (U4) and
        the `SWEEP_IN_FLIGHT` feedback (U5) need are theirs to add; this is the scoped read the
        ledger unit and its tests stand on.
        """
        return self._all("SELECT * FROM transfers WHERE household_id = :h ORDER BY seq")

    def sweeps_in_flight(self) -> Decimal:
        """The dollars pulled from checking that have not yet settled or returned — the debit legs
        whose latest state is `submitted` or `pending` (ticket 0043, U5).

        The engine reads this as `Snapshot.sweeps_in_flight` and refuses to stack a second sweep on
        an unsettled first (`decision-engine.md` [2.4]: "stacking is how you overdraft someone with
        their own money"). Counts the **debit** leg only — that is the money leaving the user's
        account; the payoff leg is downstream money already in the platform account, and counting
        both would double the same sweep. One row per `decision_date` slot (its latest debit state),
        so a settled or returned transition drops the slot out of the sum.
        """
        rows = self._all(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM ("
            " SELECT DISTINCT ON (decision_date) amount, state FROM transfers"
            " WHERE household_id = :h AND leg = 'debit'"
            " ORDER BY decision_date, seq DESC"
            ") latest WHERE state IN ('submitted', 'pending')"
        )
        return Decimal(rows[0]["total"])

    def add_transfer(
        self,
        *,
        transfer_id: str,
        target_card_id: str,
        decision_id: str,
        decision_date: date,
        leg: str,
        state: str,
        direction: str,
        amount: Decimal,
        provider: str,
        idempotency_key: str,
        provider_transfer_id: str | None = None,
        return_code: str | None = None,
    ) -> None:
        """Append one transfer-ledger transition (ticket 0039). INSERT only — the table grants the
        app role no UPDATE or DELETE, so a state transition is a new row, never a rewrite.

        `amount` is a `Decimal` (ADR-0002 [2.2]); the NUMERIC column keeps the exact cent. Through
        the repository like every other write, so RLS `WITH CHECK` binds the row to `household_id`:
        a caller that named the wrong household cannot smuggle a transfer elsewhere — the INSERT is
        rejected.
        """
        self._exec(
            "INSERT INTO transfers"
            " (id, household_id, target_card_id, decision_id, decision_date, leg, state, direction,"
            " amount, provider, provider_transfer_id, return_code, idempotency_key)"
            " VALUES (:tid, :h, :target_card_id, :decision_id, :decision_date, :leg, :state,"
            " :direction, :amount, :provider, :provider_transfer_id, :return_code,"
            " :idempotency_key)",
            tid=transfer_id,
            target_card_id=target_card_id,
            decision_id=decision_id,
            decision_date=decision_date,
            leg=leg,
            state=state,
            direction=direction,
            amount=amount,
            provider=provider,
            provider_transfer_id=provider_transfer_id,
            return_code=return_code,
            idempotency_key=idempotency_key,
        )

    def add_decision(self, **f: Any) -> None:
        self._exec(
            "INSERT INTO decisions (id, household_id, day, action, amount, target_card_id,"
            " projected_low_balance, reasons, engine_version, snapshot_ref)"
            " VALUES (:decision_id, :h, :day, :action, :amount, :target_card_id,"
            " :projected_low_balance, :reasons, :engine_version, :snapshot_ref)",
            **f,
        )

    def set_spend_projection(self, *, as_of: date, payload: str) -> None:
        """Upsert this household's spend projection. One row per household — see ticket `0031`.

        `ON CONFLICT DO UPDATE` rather than INSERT, matching `set_policy`: re-seeding must converge
        rather than raise, and a household that is walked again gets a projection describing the
        day it was walked to.

        `payload` arrives as serialized JSON text, matching `add_decision`'s `reasons` and
        `PostgresSnapshotStore.put` — psycopg will not adapt a Python container to JSONB through a
        textual bind, and Postgres casts the string on the way in.
        """
        self._exec(
            "INSERT INTO spend_projections (household_id, as_of, payload)"
            " VALUES (:h, :as_of, :payload)"
            " ON CONFLICT (household_id) DO UPDATE SET"
            " as_of = EXCLUDED.as_of, payload = EXCLUDED.payload",
            as_of=as_of,
            payload=payload,
        )

    # --- the only two places `household_id` is bound ---------------------------------

    def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        rows = self.conn.execute(text(sql), {"h": self.household_id, **params}).mappings()
        return [dict(r) for r in rows]

    def _exec(self, sql: str, **params: Any) -> None:
        self.conn.execute(text(sql), {"h": self.household_id, **params})


@contextmanager
def repository(engine: Engine, household_id: str) -> Iterator[Repository]:
    """A `Repository` inside a transaction with RLS scoped to the same household.

    Both layers, bound to the same value, in one place — so they cannot disagree. The transaction
    is what makes the RLS variable transaction-scoped rather than connection-scoped; see
    `session.household_scope`, and do not set the variable anywhere else.
    """
    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        yield Repository(conn=scoped, household_id=household_id)
