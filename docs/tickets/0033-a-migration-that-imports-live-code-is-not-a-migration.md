---
id: "0033"
title: A migration that imports live code is not a migration
type: bug
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0031-the-spend-surface-per-household.md
depends_on: []
created: 2026-07-16
completed: 2026-07-16
---

# A migration that imports live code is not a migration

**Found by trying to add a table.** Fixed inside `0031` because that ticket could not land without
it; filed because the fix is one literal and the **rule** is the thing worth keeping.

## What it was

`alembic/versions/0001_household_scoped_schema.py` did this:

```python
from backend.db.models import HOUSEHOLD_SCOPED, RLS_VAR
...
for table in ("households", *HOUSEHOLD_SCOPED):
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE};")

for table in HOUSEHOLD_SCOPED:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    ...
```

`HOUSEHOLD_SCOPED` is a **live application constant**. A migration is a statement about what
happened at a point in time; this one described whatever `backend/db/models.py` said *today*.

`0031` adds `spend_projections` to that tuple, because the IDOR suite and the schema suite
parametrize over it and a new scoped table must be covered by both. The moment it was added,
revision **0001** started trying to `GRANT ... ON spend_projections` — three revisions before
`0004` creates it.

## Why nobody noticed

`HOUSEHOLD_SCOPED` had not changed since `0001` was written. The coupling was invisible for exactly
as long as the list stood still.

And the failure is **silent where it matters**: every already-migrated database is at `0004` and
never replays `0001`. Only a migration **from zero** breaks — a new developer, a fresh Neon branch,
a restored-from-nothing environment, a `downgrade base` + `upgrade head`. CI never migrates from
zero against a used database either.

This is the same shape as every defect this plan has turned up, and the ticket README already names
it: **a mechanism that was built, tested, and never actually exercised.** No symptom, green tests.

## Verified by mutation, not by reading

```
with 0001 coupled to the live constant (the pre-fix code):
  psycopg.errors.UndefinedTable: relation "spend_projections" does not exist
with the freeze restored:
  0004 (head)
```

## The fix, and the rule

`0001` names the tables that existed **at that revision**, as a literal:

```python
_SCOPED_AT_0001 = ("accounts", "cards", "policies", "decisions", "snapshots")
```

**The rule, for every migration after this one:**

- A migration states what **it** did. It names its own objects, literally.
- The application constant (`HOUSEHOLD_SCOPED`) states what must be scoped **now**. It is the
  source of truth for the *tests* — `test_schema.py` and `test_idor.py` parametrize over it, which
  is what catches a new table with no policy — and for nothing in `alembic/`.
- `RLS_VAR` and `APP_ROLE` are still imported, and that is fine: they are **names**, not a list of
  objects that grows. If either ever became a thing that changes per-revision, it would need
  freezing too.

## Acceptance criteria

- [x] `0001` no longer imports `HOUSEHOLD_SCOPED`; its list is a literal with a comment saying why.
- [x] `0004` names its own table rather than reading the constant.
- [x] `alembic upgrade head` from an empty database reaches `0004`.
- [x] Mutation-tested: restoring the import reproduces the failure.

## Follow-up worth considering — and there is precedent now

**Nothing enforces this rule.** A future migration can import the constant again and it will pass
every test, for the same reason this one did — until the constant next moves. A CI job that migrates
a **fresh** database from zero to head would catch the whole class, and it is the check this repo
does not have: `tests/test_schema.py` asserts the schema is *right*, never that it is *reachable
from nothing*.

**This is the second instance of one shape, not the first.** PR #50 found the same thing wearing
different clothes: `backend/requirements.txt` and `pyproject.toml`'s `[api]` extra are two lists of
the same fact, nothing compared them, and they disagreed for eleven tickets while CI stayed green.
Here it is `_SCOPED_AT_0001` and `HOUSEHOLD_SCOPED`. **Two sources of truth, no comparison, green
tests** — and in both cases the only environment that could see it was the one nobody runs (a fresh
database; the deploy).

#50 answered its instance with `tests/test_requirements.py` rather than by waiting for a third. That
is the precedent: an equivalent check here is a CI job that runs `alembic upgrade head` against an
empty database. `0020` already gives CI a `postgres:16` service container, so the cost is one job
that creates a database and migrates it — which would also, for free, be the first thing in this
repo to prove the migrations are runnable from scratch at all.

Not built here: this ticket's fix is one literal, and adding a CI job to a branch that is already
reopening a merged ticket is scope this does not need. Filed as the follow-up rather than the work.
