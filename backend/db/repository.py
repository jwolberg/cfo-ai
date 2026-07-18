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

import uuid
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

    def archetype(self) -> str | None:
        """This household's archetype, or `None` for a real (linked) household — the column's own
        meaning (`backend/readpath.py:Household.label`). The live-assembly path (ticket 0056) reads
        it to tell a **frozen** seeded demo household, served from stored snapshots, from a
        **linked** one that must be re-decided from its current policy and attestation.

        Readable under the scoped repo because `households` carries no RLS — it is the tenant
        registry (`add_plaid_item`'s note), the one table read before there is a household to scope
        to. A `None` return is a **linked** household (or, before `authorize_household` has run, a
        missing one); the live route only reaches here past membership authorization, so there the
        household exists and `None` means linked.
        """
        rows = self._all("SELECT archetype FROM households WHERE id = :h")
        return rows[0]["archetype"] if rows else None

    def policy(self) -> dict[str, Any] | None:
        """The current guardrails — the **latest** `policy_events` row for this household (KTD-6).

        Ordered by `seq`, not `created_at`: a seed transaction can write the initial event and a
        later one in the same wall-clock instant, and the state's whole meaning is *which is latest*
        (the `transfers.seq` lesson). Returns the projection of an append-only history, not a
        mutable row — there is no second copy that could disagree with the audit trail.
        """
        rows = self._all(
            "SELECT * FROM policy_events WHERE household_id = :h ORDER BY seq DESC LIMIT 1"
        )
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

    def set_policy(
        self,
        *,
        buffer_floor: Decimal,
        max_sweep: Decimal,
        max_weekly_sweep: Decimal,
        min_days_between_sweeps: int,
        blackout_dates: list[str],
        changed_by: str | None = None,
        loosened: bool = False,
    ) -> None:
        """Append a policy change (ticket 0049, KTD-6). **An INSERT, never an upsert** — the mutable
        `policies` row was retired; the current policy is the latest event `policy()` reads.

        `changed_by` is the user who made the change (NULL for the seeder's initial event);
        `loosened` flags a change that weakened a guardrail — the audit marker the step-up retrofit
        finds them by (KTD-9). The append-only grant means even this method cannot rewrite history:
        a correction is another event, and `policy()` simply reads the newest.
        """
        self._exec(
            "INSERT INTO policy_events (id, household_id, changed_by, buffer_floor, max_sweep,"
            " max_weekly_sweep, min_days_between_sweeps, blackout_dates, loosened)"
            " VALUES (:id, :h, :changed_by, :buffer_floor, :max_sweep, :max_weekly_sweep,"
            " :min_days_between_sweeps, :blackout_dates, :loosened)",
            id=f"pe_{uuid.uuid4().hex}",
            changed_by=changed_by,
            buffer_floor=buffer_floor,
            max_sweep=max_sweep,
            max_weekly_sweep=max_weekly_sweep,
            min_days_between_sweeps=min_days_between_sweeps,
            blackout_dates=blackout_dates,
            loosened=loosened,
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

    def add_plaid_account(self, **f: Any) -> None:
        """Append one account-balance snapshot (migration 0014). INSERT only — a refresh is a new
        row, and the reader (`latest_plaid_accounts`) takes the newest by `seq`."""
        self._exec(
            "INSERT INTO plaid_accounts"
            " (id, household_id, plaid_item_id, plaid_account_id, name, official_name, type,"
            " subtype, current_balance, available_balance, iso_currency_code)"
            " VALUES (:id, :h, :plaid_item_id, :plaid_account_id, :name, :official_name, :type,"
            " :subtype, :current_balance, :available_balance, :iso_currency_code)",
            id=f"pa_{uuid.uuid4().hex}",
            **f,
        )

    def add_plaid_liability(self, **f: Any) -> None:
        """Append one card-terms snapshot (migration 0014). INSERT only, latest-by-`seq`.

        `purchase_apr` is a fraction (0.2399), not a percentage — the engine's `apr` convention. The
        caller converts Plaid's percentage; an unknown APR is `None` (the estimated case, 0028)."""
        self._exec(
            "INSERT INTO plaid_liabilities"
            " (id, household_id, plaid_item_id, plaid_account_id, last_statement_balance,"
            " last_statement_issue_date, minimum_payment, next_payment_due_date, purchase_apr,"
            " is_overdue)"
            " VALUES (:id, :h, :plaid_item_id, :plaid_account_id, :last_statement_balance,"
            " :last_statement_issue_date, :minimum_payment, :next_payment_due_date, :purchase_apr,"
            " :is_overdue)",
            id=f"pl_{uuid.uuid4().hex}",
            **f,
        )

    def latest_plaid_accounts(self) -> list[dict[str, Any]]:
        """The newest balance snapshot **per account** — one row for each `plaid_account_id`.

        `DISTINCT ON` ordered by `seq DESC` so a re-fetch supersedes the prior snapshot without
        rewriting it: the history stays in the table, and the reader sees only the current balance.
        Ordered by `seq`, not `fetched_at`, for the reason the table's own comment gives.
        """
        return self._all(
            "SELECT DISTINCT ON (plaid_account_id) * FROM plaid_accounts"
            " WHERE household_id = :h ORDER BY plaid_account_id, seq DESC"
        )

    def latest_plaid_liabilities(self) -> list[dict[str, Any]]:
        """The newest card-terms snapshot per account — the mirror of `latest_plaid_accounts`."""
        return self._all(
            "SELECT DISTINCT ON (plaid_account_id) * FROM plaid_liabilities"
            " WHERE household_id = :h ORDER BY plaid_account_id, seq DESC"
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

    def member_role(self, user_id: str) -> str | None:
        """This user's role in *this* household (`owner`/`viewer`), or None if not a member.

        A scoped read: RLS makes only this household's membership rows visible, so a caller can read
        their own role and no one else's. The write dependencies gate on this — `owner` may write,
        `viewer` may not (ticket 0047, KTD-10)."""
        rows = self._all(
            "SELECT role FROM household_members WHERE household_id = :h AND user_id = :u",
            u=user_id,
        )
        return rows[0]["role"] if rows else None

    def add_attestation(self, *, card_fingerprint: str, attested_by: str | None = None) -> None:
        """Append a card-completeness attestation for this household (ticket 0050, KTD-7).

        An INSERT, never an upsert — the append-only grant means an attestation is a new row and
        `current_attestation()` reads the latest. `card_fingerprint` is the stable hash of the
        attested card set; a later new card changes the current set's fingerprint and the match in
        `attested_for` fails, dropping coverage back to `UNATTESTED` — the correct safety move."""
        self._exec(
            "INSERT INTO card_attestations (id, household_id, attested_by, card_fingerprint)"
            " VALUES (:id, :h, :attested_by, :fp)",
            id=f"att_{uuid.uuid4().hex}",
            attested_by=attested_by,
            fp=card_fingerprint,
        )

    def current_attestation(self) -> dict[str, Any] | None:
        """The latest attestation for this household, or None — read by `seq`, not `created_at`."""
        rows = self._all(
            "SELECT * FROM card_attestations WHERE household_id = :h ORDER BY seq DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def add_membership(self, *, user_id: str, role: str) -> None:
        """Add (or re-role) a user's membership in *this* household (ticket 0046).

        A Repository method because `household_members` is HOUSEHOLD_SCOPED: the INSERT rides RLS
        `WITH CHECK`, so a session scoped to household A cannot smuggle a membership into B — the
        row is bound to `self.household_id` and any other value is rejected. `ON CONFLICT DO UPDATE`
        rather than raising, so re-seeding converges and a role change is idempotent (matching
        `set_policy`). `user_id` is a bridge FK; the household stays the tenant.
        """
        self._exec(
            "INSERT INTO household_members (household_id, user_id, role)"
            " VALUES (:h, :user_id, :role)"
            " ON CONFLICT (household_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            user_id=user_id,
            role=role,
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


# --- users: platform-level, reached ONLY by single key (ticket 0046, KTD-1) ----------
#
# `users` is deliberately outside `HOUSEHOLD_SCOPED` (`backend/db/models.py`): a user predates every
# household, so there is no household to scope the query to, and these are free functions on a raw
# connection rather than methods on the household-scoped `Repository`.
#
# It carries PII with no RLS backstop, so the discipline that replaces RLS is: **every read is by a
# unique key** (`id` or `stytch_user_id`), returning at most one row. There is no "list all users"
# function here on purpose — an unfiltered scan is exactly the cross-user PII leak a later admin
# route could add by accident, and `tests/test_identity_schema.py` asserts this module exposes no
# such call.


def add_user(
    conn: Connection, *, user_id: str, stytch_user_id: str, email: str | None = None
) -> None:
    """Insert a user, idempotently on `stytch_user_id` (ticket 0046).

    `ON CONFLICT (stytch_user_id) DO NOTHING` makes JIT provisioning safe under the concurrent
    first-login race: two requests bearing the same brand-new session both try to provision, and the
    second becomes a no-op rather than a unique-violation. The caller re-reads with
    `get_user_by_stytch_id` to obtain the row either request won.
    """
    conn.execute(
        text(
            "INSERT INTO users (id, stytch_user_id, email) VALUES (:id, :sid, :email)"
            " ON CONFLICT (stytch_user_id) DO NOTHING"
        ),
        {"id": user_id, "sid": stytch_user_id, "email": email},
    )


def get_user_by_stytch_id(conn: Connection, stytch_user_id: str) -> dict[str, Any] | None:
    """The one row for a Stytch user, or None. Single-key lookup — the only sanctioned user read."""
    row = (
        conn.execute(
            text("SELECT * FROM users WHERE stytch_user_id = :sid"), {"sid": stytch_user_id}
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def get_user_by_id(conn: Connection, user_id: str) -> dict[str, Any] | None:
    """The one row for our user id, or None. Single-key lookup — the only other sanctioned read."""
    row = (
        conn.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    )
    return dict(row) if row else None


def households_for_user(conn: Connection, user_id: str) -> list[str]:
    """The household ids a user belongs to, via the SECURITY DEFINER lookup (ticket 0046, ADR-0008).

    The membership lookup has the same chicken-and-egg as the webhook doorbell (ADR-0005): to know
    which household to scope to we must read `household_members`, but it is FORCE'd, so an unscoped
    app session sees nothing in it. `households_for_user(text)` runs as its owner and reads it for
    this one narrow purpose, returning only ids. This is the seam `authorize_household` (U2) stands
    on: the requested `household_id` must be in this set or the route refuses.
    """
    rows = (
        conn.execute(text("SELECT households_for_user(:u) AS household_id"), {"u": user_id})
        .scalars()
        .all()
    )
    return list(rows)


def create_household(engine: Engine, *, owner_user_id: str) -> str:
    """Create a **real** (non-demo) household and make `owner_user_id` its owner. The onboarding
    write path the identity rung deferred (ticket 0046, Decision 3) — signup → *create household* →
    link a bank.

    `archetype = NULL` and `is_demo = false`: this is a real household, never the public demo plane
    (KTD-10), so a real bank Item can attach to it (`backend/plaid/link.py` refuses `is_demo`). The
    two writes ride **one transaction** under the new household's scope: the `households` INSERT
    needs no RLS (it is the tenant registry), and the `household_members` INSERT rides RLS
    `WITH CHECK` bound to this id — so the membership can only ever attach to the household just
    created, never someone else's. If either fails, neither lands, and there is no orphan household
    with no owner (which would be a household nobody — not even its creator — could ever reach).
    """
    household_id = f"hh_{uuid.uuid4().hex}"
    with engine.connect() as conn, household_scope(conn, household_id) as scoped:
        scoped.execute(
            text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, NULL, false)"),
            {"h": household_id},
        )
        Repository(conn=scoped, household_id=household_id).add_membership(
            user_id=owner_user_id, role="owner"
        )
    return household_id
