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
