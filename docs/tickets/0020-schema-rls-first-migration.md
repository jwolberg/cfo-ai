---
id: "0020"
title: Schema, RLS, and the first migration
type: feature
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: []
created: 2026-07-16
---

# Schema, RLS, and the first migration

Implements **U2** of the plan. Single owner: `backend-python-agent`.

**Depends on:** nothing. Runs in parallel with `0019`.

**Files:** `backend/db/models.py`, `backend/db/session.py`, `alembic/`, `pyproject.toml`,
`.github/workflows/`, `tests/test_schema.py`

Read [ADR-0004](../decisions/0004-postgres-scoped-by-household.md) and
[`architecture.md`](../architecture.md) [4]/[4.1] first. Every column here is one of those two
documents' decisions.

## The schema

```sql
households (id, archetype, dek_id, created_at, deleted_at)
accounts   (id, household_id FK, kind, balance NUMERIC, connection, balance_age_days)
cards      (id, household_id FK, apr NUMERIC NULL, statement_balance NUMERIC,
            statement_due_date, minimum_payment NUMERIC, unbilled_balance NUMERIC,
            next_close_date, behavior, observed_monthly_payment NUMERIC NULL,
            observed_monthly_charges NUMERIC NULL)
policies   (household_id FK, buffer_floor NUMERIC, max_sweep NUMERIC,
            max_weekly_sweep NUMERIC, min_days_between_sweeps INT, blackout_dates DATE[])
decisions  (id, household_id FK, day, action, amount NUMERIC, target_card_id,
            projected_low_balance NUMERIC NULL, reasons JSONB, engine_version,
            snapshot_ref TEXT)                        PARTITION BY RANGE (day)
snapshots  (id, household_id FK, payload JSONB)       -- reached only via 0022's seam
```

Mirror `engine/models.py` exactly — `Account`, `Card`, `UserPolicy`, `Decision`. Where the engine
says `Decimal | None`, the column is `NUMERIC NULL`. **`cards.apr` is nullable and that is
load-bearing** (`engine/decide.py:199-204` refuses to rank rather than guess).

## Four things that are not negotiable

**1. `NUMERIC`, never `float`/`double precision`.** ADR-0002 [2.2] exists entirely because "a cent
that round-trips through a float is no longer the cent the engine decided on." `NUMERIC` is exact
natively — this is the one place the new layer is *stronger* than the artifact. `money()` remains the
only sanctioned constructor.

**2. `household_id`, not `user_id`.** `architecture.md` [4] carried `user_id` until 2026-07-16 and
was corrected. A `user` is a login; the household is the tenant. **`users` is not created here.**

**3. RLS on every household-scoped table**, keyed off `current_setting('app.household_id')`. Enable
`FORCE ROW LEVEL SECURITY` so the table owner is not exempt — a policy the application role bypasses
is decoration.

**4. `PARTITION BY RANGE (day)` on `decisions`, monthly.** Free at zero rows; a migration scheduled
around a 365M-row table later. Include partition creation for the seeded window and a documented
path for making the next one.

## New dependencies, justified

`sqlalchemy` (**Core, not the ORM**), `alembic`, `psycopg[binary]` — added to the `api` extra.
`engine/` and `sim/` keep `dependencies = []`; `pyproject.toml`'s comment stands: *"engine/sim stay
dependency-free — this is the web shell around them."*

ADR-0002 celebrated "no ORM." Core is not the ORM: the SQL stays visible. Alembic because a schema
without migrations drifts. Logged in `docs/implementation-notes.md`.

## CI

Add a `postgres:16` service container to the backend workflow. **CI must not point at Neon** —
secrets, cost, and flakiness. Neon is stock Postgres, so the drift is nil, and the deployed instance
is `0026`'s problem.

## Acceptance criteria

- [ ] `alembic upgrade head` and `downgrade base` both clean, from empty.
- [ ] Every money column is `NUMERIC`. A test round-trips a `Decimal` through each and asserts
      **exact** equality — including a value that would lose precision as a float
      (e.g. `Decimal("0.1")`, `Decimal("1234567.89")`).
- [ ] RLS is enabled **and forced** on every household-scoped table. A test asserts a session with
      `app.household_id = A` sees zero rows of B's, **at the SQL layer with no repository involved**
      (the repository is `0021`).
- [ ] `decisions` is partitioned; a test inserts across a partition boundary and reads both back.
- [ ] CI runs the schema tests against `postgres:16`.
- [ ] `pytest` and `ruff` clean.

## Out of scope

`items`, `transactions`, `recurring_events`, `payments`, `users`. Named in `architecture.md` [4],
shape not yet known — **empty tables invite guessed columns.** The migration is cheap once Plaid
makes the shape real.

Real KMS wiring. `dek_id` is a column with no key behind it; synthetic households have no PII. The
column is the expensive half (ADR-0004 [2]).
