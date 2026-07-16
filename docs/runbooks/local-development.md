# Running the tests locally

Since ticket `0020` the suite needs two things it did not need before: a **virtualenv** and a
**real Postgres**. Neither is optional and neither is discoverable, so they are written down here.

---

## Use the venv, not the system Python

```bash
.venv/bin/python -m pytest        # not: python3 -m pytest
```

If `.venv/` does not exist:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

**Why a venv, when this repo went without one for months.** The project now needs
`sqlalchemy`/`alembic`/`psycopg` (`0020`). On at least one developer machine the system Python
already holds SQLAlchemy **2.0.30** for `Flask-SQLAlchemy`, `langchain`, and `langchain-community`
— unrelated projects. Upgrading it to reach this project's floor would reach *outside this repo*
and change those. The venv keeps them apart, and it matches what CI installs fresh via
`pip install -e ".[dev]"`.

`python3 -m pytest` still works and still passes — it just **skips** every database test, loudly.
That is fine for a quick engine change and useless for anything touching `backend/db/`.

---

## The database suites need a Postgres, and will say so

`tests/test_schema.py`, `tests/test_idor.py`, and part of `tests/test_snapshots.py` skip unless
`TEST_DATABASE_URL` is set. **They do not run against SQLite** — RLS and declarative partitioning
are the subject, so a stand-in would test a different artifact and report green.

A throwaway instance, owned by nothing and started on a non-default port:

```bash
export LC_ALL=C LANG=C            # macOS: without this the postmaster dies "multithreaded during startup"
PG=/tmp/cfo-pgdata
initdb -D "$PG" -U postgres --auth=trust -E UTF8 --locale=C
pg_ctl -D "$PG" -o "-p 55432 -k /tmp" -l "$PG/server.log" start
createdb -h 127.0.0.1 -p 55432 -U postgres cfo_ai_test
```

> ⚠️ **`-E UTF8` is not decoration.** `initdb` under `LC_ALL=C` defaults the database to
> `SQL_ASCII`, and **psycopg returns `bytes` instead of `str` on a SQL_ASCII connection** — which
> surfaces a thousand lines away as `TypeError: cannot use a string pattern on a bytes-like object`
> inside SQLAlchemy's dialect, at connect time. It reads exactly like a driver version conflict. It
> is not. It is the encoding.

Then:

```bash
export TEST_DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:55432/cfo_ai_test"
export RESFI_API_KEY=local-not-real ANTHROPIC_API_KEY=sk-ant-local-not-real
.venv/bin/python -m pytest
```

`RESFI_API_KEY` and `ANTHROPIC_API_KEY` are required because the service refuses to start without
them — deliberately (`backend/auth.py`). Neither value calls anything; the Anthropic client is a
fake in every test that exercises the assistant.

The suite migrates the test database itself (`tests/conftest.py`), so there is no separate
`alembic upgrade` step. It runs **Alembic**, not `metadata.create_all()`, on purpose: `create_all`
would silently skip the partitioning and the RLS policies, which are the entire subject.

Expected: **448 passed, 1 skipped** with a database; **~375 passed, ~73 skipped** without.

---

## Two shapes of test that look wrong and are not

**Skips are loud and CI cannot have them.** `tests/test_ci_runs_the_db_suite.py` fails the build if
`TEST_DATABASE_URL` is unset in CI. A skipped security test reports green while proving nothing —
`prd.md` §5.2's lesson one level down.

**The IDOR suite deliberately runs as three different roles.** `db_engine` is the superuser (used to
*arrange* data); `app_engine` is a non-superuser (used to *assert* isolation). A superuser bypasses
RLS outright, **even when it is FORCEd**, so an isolation test written on `db_engine` passes while
proving nothing at all. That is not hypothetical — it is what the first draft did.

---

## `backend/data/decisions.json` must never be regenerated to make a test pass

`tests/test_precompute.py` compares the committed artifact against a fresh `build()`. If it goes
red after a refactor, **the refactor is wrong — not the file**. Regenerating it deletes the only
evidence the change preserved behaviour, and it is the regression oracle three tickets have leaned
on (`0019`, `0027`, `0028`).

The same goes for `calibrate`. It should read **4,320 graded days, 2.338% breach, 0 sweep-caused
overdrafts, $544,640.58** false-refusal cost:

```bash
.venv/bin/python -c "from backend.calibrate import measure; r = measure(None); print(r.graded_days, r.breach_rate, r.sweep_caused_overdrafts, r.false_refusal_cost)"
```

Those numbers are cited in `prd.md`, `strategy.md`, and `decision-engine.md`. A change that moves
them is a change to three documents, and it needs to be **stated**, not absorbed.

---

## Neon

See [`neon-provisioning.md`](./neon-provisioning.md). The short version, and the part that will bite:
**`neondb_owner` has `rolbypassrls = true`** and must never be the runtime role. `assert_rls_binds()`
refuses to start under it.

Nothing local needs Neon. The migration is already applied there.
