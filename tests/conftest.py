"""Database fixtures for the schema and IDOR suites.

**Skipping is loud on purpose.** A database test that silently skips is worse than one that fails:
it reports green while proving nothing, which is the exact failure `docs/prd.md` §5.2 is about one
level down. `TEST_DATABASE_URL` gates them locally; CI always sets it, and
`test_ci_actually_runs_the_database_tests` fails if CI ever stops.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

TEST_DB_ENV = "TEST_DATABASE_URL"

_SKIP = (
    f"{TEST_DB_ENV} is not set — the schema and IDOR suites need a real Postgres.\n"
    "  Locally:  see docs/RUNBOOK.md, or run a throwaway instance and export\n"
    f"            {TEST_DB_ENV}=postgresql+psycopg://postgres@127.0.0.1:5432/cfo_ai_test\n"
    "  CI:       set by .github/workflows/ci.yml against the postgres service container.\n"
    "  These do NOT run against SQLite: RLS and declarative partitioning are the subject."
)


def _url() -> str | None:
    return os.environ.get(TEST_DB_ENV)


requires_db = pytest.mark.skipif(_url() is None, reason=_SKIP)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    url = _url()
    if url is None:
        pytest.skip(_SKIP)

    engine = create_engine(url, future=True)

    # Migrate once per session. The suite tests the schema Alembic produces, not a schema
    # `metadata.create_all()` produces — those are different artifacts, and only one of them ever
    # runs in production. `create_all()` would silently skip the partitioning and the RLS policies,
    # which are the entire subject of these tests.
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    os.environ["DATABASE_URL"] = url
    command.upgrade(cfg, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Connection]:
    """A connection with the tables emptied. Superuser — RLS does **not** bind here.

    Use this to arrange data. To *assert* isolation, use `as_app` — a superuser bypasses RLS
    outright, even when it is FORCEd, so an isolation test written on this fixture would pass
    while proving nothing at all.
    """
    with db_engine.connect() as conn:
        with conn.begin():
            # `decisions` is named explicitly and must stay that way. It has **no foreign key** to
            # `households` — a partitioned table's FKs constrain partition maintenance, so
            # `architecture.md` [4]'s "repository layer AND row-level security" is what scopes it
            # instead. The cost lands right here: `TRUNCATE households CASCADE` does not reach it,
            # and a fixture that assumed otherwise leaves rows behind and fails the *next* test
            # with a duplicate key from a household it never created.
            conn.execute(text("TRUNCATE households, decisions CASCADE"))
        yield conn


@pytest.fixture(scope="session")
def app_engine(db_engine: Engine) -> Iterator[Engine]:
    """An engine connected as a **non-superuser** — the only kind RLS applies to.

    This fixture exists because the obvious setup is silently useless. `db_engine` connects as the
    superuser that owns the tables, and a superuser bypasses row-level security outright — even
    when it is FORCEd. Every "scoped" query through it returns every household's rows, so an
    isolation test written on `db_engine` passes while proving nothing. That is not hypothetical:
    it is what the first draft of `tests/test_idor.py` did, and the suite caught it.

    `cfo_test` is a LOGIN role holding `cfo_app`'s privileges by membership. That mirrors
    production rather than working around it: `cfo_app` is NOLOGIN because it is an application
    role, not a person, and the deployed service connects as a Neon role that has been granted it
    (ticket 0026). The RLS policies carry no `TO` clause, so they bind every non-superuser role —
    which is the property that makes this substitution honest.
    """
    from sqlalchemy import make_url

    url = make_url(_url())
    with db_engine.connect() as conn, conn.begin():
        _drop_test_role(conn)
        conn.execute(text("CREATE ROLE cfo_test LOGIN PASSWORD 'cfo_test' IN ROLE cfo_app"))
        # The role must be able to reach the tables, but must NOT own them and must not be
        # superuser or BYPASSRLS — any of the three would make this fixture a superuser in
        # disguise. `tests/test_idor.py` asserts all three.
        conn.execute(text("GRANT USAGE ON SCHEMA public TO cfo_test"))

    engine = create_engine(url.set(username="cfo_test", password="cfo_test"), future=True)
    yield engine
    engine.dispose()

    with db_engine.connect() as conn, conn.begin():
        _drop_test_role(conn)


def _drop_test_role(conn: Connection) -> None:
    """Drop `cfo_test`, grants and all.

    `DROP OWNED BY` is not optional and not defensive noise: Postgres refuses to drop a role that
    still holds a single privilege, and `GRANT USAGE ON SCHEMA public` is one. Without this the
    teardown dies with "role cfo_test cannot be dropped because some objects depend on it:
    privileges for schema public" and the next run inherits a half-configured role.

    The same rule sank the first draft of `alembic/versions/0001`'s `downgrade()`, which is why
    that migration no longer drops `cfo_app` at all — a role is cluster-wide and a migration is
    database-scoped. Here the role is created and dropped in the same session against the same
    database, so dropping it is honest.
    """
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'cfo_test'")).scalar()
    if exists:
        conn.execute(text("DROP OWNED BY cfo_test"))
        conn.execute(text("REVOKE ALL ON SCHEMA public FROM cfo_test"))
        conn.execute(text("DROP ROLE cfo_test"))


@pytest.fixture
def as_app(db_engine: Engine):
    """Run a callable as `cfo_app`, scoped to one household. **This is where RLS binds.**

    `SET LOCAL ROLE` rather than a separate login: `cfo_app` is NOLOGIN (it is an application
    role, not a person), and RLS is evaluated against the *current* role — so becoming it inside
    the transaction is what makes the policy apply. A superuser would bypass it entirely.

    Both the role and the household are transaction-scoped and released with it — the property
    `tests/test_idor.py` exists to hold us to. The household goes through `set_config(..., true)`
    rather than `SET LOCAL` because `SET` cannot take a bind parameter; see
    `backend/db/session.py`'s docstring, which is the code this mirrors.
    """
    from backend.db.models import RLS_VAR
    from backend.db.session import APP_ROLE

    def run(household_id: str | None, fn):
        with db_engine.connect() as conn, conn.begin():
            conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
            if household_id is not None:
                conn.execute(
                    text("SELECT set_config(:v, :h, true)"),
                    {"v": RLS_VAR, "h": household_id},
                )
            return fn(conn)

    return run
