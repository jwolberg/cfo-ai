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

    def add_decision(self, **f: Any) -> None:
        self._exec(
            "INSERT INTO decisions (id, household_id, day, action, amount, target_card_id,"
            " projected_low_balance, reasons, engine_version, snapshot_ref)"
            " VALUES (:decision_id, :h, :day, :action, :amount, :target_card_id,"
            " :projected_low_balance, :reasons, :engine_version, :snapshot_ref)",
            **f,
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
