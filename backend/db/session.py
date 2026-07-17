"""Connecting, and the one rule about how.

`DATABASE_URL` is read from the environment at startup and the process refuses to come up
without it — the same posture `backend/auth.py` takes with `RESFI_API_KEY`, and for the same
reason ADR-0002 [2.1] gave: a service holding bad data should not serve wrong numbers one request
at a time.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine, text

from backend.db.models import RLS_VAR

# The role the application connects as. It must NOT be the table owner and must NOT be superuser:
# RLS does not apply to either without FORCE, and "the policy exists" is not the same claim as
# "the policy binds us". The migration creates it and forces RLS anyway — belt and braces, because
# ticket 0021's IDOR suite is only meaningful if both are true.
APP_ROLE = "cfo_app"


class DatabaseNotConfigured(RuntimeError):
    """Raised at startup when `DATABASE_URL` is absent. Never raised per-request."""


class RlsWouldNotBind(RuntimeError):
    """Raised at startup when the connection role can bypass row-level security."""


def assert_rls_binds(conn: Connection) -> None:
    """Refuse to start if this connection's role bypasses RLS. Call once, at startup.

    **This is not defensive programming. Neon's default role fails it.**

    A `SUPERUSER` or `BYPASSRLS` role ignores every policy on every table, silently and with no
    error — `household_scope()` still sets the variable, the policies still exist, `pg_policies`
    still lists them, and every query returns every household's rows anyway. There is no symptom
    until someone reads someone else's financial life.

    Measured against the real thing on 2026-07-16: Neon provisions `neondb_owner` with
    `rolbypassrls = true`. Connected as that role and scoped to one household, a `SELECT` returned
    **both** households. So the obvious deployment — paste the connection string Neon hands you into
    `DATABASE_URL` — turns `architecture.md` [4]'s defense-in-depth into one layer, and turns
    `tests/test_idor.py` into a suite that proves a property production does not have.

    Hence a runtime role that is neither (`cfo_runtime`, granted `cfo_app`; see `DEPLOY.local.md`),
    and hence this check: the schema's guarantees are only worth what the *connecting role* makes
    them worth, and that is a deploy-time fact no test in CI can see.
    """
    row = conn.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    if row.rolsuper or row.rolbypassrls:
        who = conn.execute(text("SELECT current_user")).scalar()
        raise RlsWouldNotBind(
            f"the database role {who!r} has "
            f"{'SUPERUSER' if row.rolsuper else ''}"
            f"{' and ' if row.rolsuper and row.rolbypassrls else ''}"
            f"{'BYPASSRLS' if row.rolbypassrls else ''}"
            " and therefore ignores every row-level security policy. Every household would be "
            "readable by every request. Connect as a role that is neither — see DEPLOY.local.md's "
            "Neon provisioning. Neon's own neondb_owner has BYPASSRLS and must not be the runtime "
            "role."
        )


class PlaidAccessTokenWouldLeak(RuntimeError):
    """Raised at startup when a real Plaid access_token would be stored in plaintext."""


def plaid_env() -> str:
    """The Plaid environment, defaulting to the safe one.

    `sandbox` is the default because it is the only environment where a plaintext access_token is
    harmless — the token grants access to fabricated data and can drain nothing. Any other value
    means the tokens on `plaid_items.access_token` are real credentials.
    """
    return os.environ.get("PLAID_ENV", "sandbox").strip().lower()


def _plaid_token_encryption_active(conn: Connection) -> bool:
    """Whether Plaid access tokens are encrypted at rest by a live key. False until KMS lands.

    The signal is the encryption *capability*, deliberately **not** `households.dek_id`. A `dek_id`
    with no key behind it — which is every synthetic household today (`backend/db/models.py`) — is
    exactly the false "looks protected" this refuses to be fooled by: a presence-check on `dek_id`
    would pass while the token stayed plaintext, which is the whole failure this guard exists to
    stop (brainstorm [5.1]).

    When KMS envelope encryption lands (`architecture.md` [7.2], the first real token), this returns
    True only when a KMS client initializes against its configured key AND `access_token` is stored
    as ciphertext — and this function moves with that code. Until then it is honestly False: a
    non-sandbox deploy cannot keep a real token safe, so it must not come up. `conn` is taken now so
    the future ciphertext check has the connection it will need.
    """
    return False


def assert_plaid_tokens_safe_at_rest(conn: Connection) -> None:
    """Refuse to start if a real Plaid access_token would sit in plaintext. Call once, at startup.

    **The trigger fires quietly, which is the reason this exists.** In Sandbox the access_token
    protects nothing, so it is stored in plaintext and KMS is deferred (`architecture.md` [7.2]).
    The day `PLAID_ENV` flips to `production` the *same column* becomes a live credential that can
    drain a household — with no migration to force the question and no failing test to notice. This
    is that failing test, made a startup gate, in the same shape as `assert_rls_binds()` above.

    Keyed on a real encryption signal, never on `households.dek_id`: a stray non-null `dek_id` must
    not satisfy it while the token stays plaintext (see `_plaid_token_encryption_active`). The
    interim invariant, explicit until KMS lands: nothing sets `dek_id` before the encryption path
    that consumes it.
    """
    if plaid_env() == "sandbox":
        return
    if not _plaid_token_encryption_active(conn):
        raise PlaidAccessTokenWouldLeak(
            f"PLAID_ENV is {plaid_env()!r}, so Plaid access tokens are real credentials — but "
            "envelope encryption is not active, so plaid_items.access_token would be stored in "
            "plaintext. KMS must be live and the token stored as ciphertext before a non-sandbox "
            "boot (architecture.md [7.2]). Refusing to start rather than persist a credential that "
            "can drain a household in the clear. A non-null dek_id is NOT sufficient: it has no "
            "key behind it yet."
        )


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set. The service refuses to start without a database rather "
            "than failing one request at a time."
        )
    return url


def make_engine(url: str | None = None) -> Engine:
    # `pool_pre_ping` because Neon scales to zero: the first query after an idle period meets a
    # connection the pool believes is alive and the server has long since forgotten.
    return create_engine(url or database_url(), pool_pre_ping=True, future=True)


@contextmanager
def household_scope(conn: Connection, household_id: str) -> Iterator[Connection]:
    """Run a transaction with RLS scoped to one household.

    ⚠️ **Transaction-scoped, never connection-scoped.** This is the whole point of this function
    existing, and the trap ticket 0021 warns about.

    Neon pools connections. A plain `SET app.household_id` persists for the life of the
    *connection*, not the transaction, so the next checkout from the pool inherits it — an IDOR
    wearing a security feature's clothes. A test suite that opens a single connection and reuses
    it will never catch the difference, which is why `tests/test_idor.py` asserts the variable does
    not survive a returned connection specifically.

    **`set_config(..., is_local => true)` rather than `SET LOCAL`, and not as a style choice.**
    `SET` is a utility statement and **cannot take a bind parameter** — `SET LOCAL app.household_id
    = :hid` is a syntax error, so the only way to write it is to interpolate `household_id` into
    the SQL string. That value arrives from a request. `set_config` is an ordinary function, takes
    the value as a parameter, and its `is_local` argument means exactly `SET LOCAL`: scoped to the
    transaction, released with it.

    So the safe spelling and the correct spelling are the same one. The obvious spelling is
    neither.
    """
    with conn.begin():
        conn.execute(
            text("SELECT set_config(:var, :hid, true)"),
            {"var": RLS_VAR, "hid": household_id},
        )
        yield conn


def current_household(conn: Connection) -> str | None:
    """Whatever `RLS_VAR` is set to on this connection right now, or None.

    Exists for the leak test in `tests/test_idor.py`. `current_setting(..., true)` returns NULL
    rather than raising when the variable was never set, which is the case that matters: a
    connection fresh from the pool must carry nothing.
    """
    got = conn.execute(text(f"SELECT current_setting('{RLS_VAR}', true)")).scalar()
    return got or None
