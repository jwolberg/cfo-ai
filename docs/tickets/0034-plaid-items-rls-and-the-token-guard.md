---
id: "0034"
title: plaid_items, RLS, and the access_token start guard
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md
depends_on: []
created: 2026-07-17
completed: 2026-07-17
---

# plaid_items, RLS, and the access_token start guard

The first unit of the Plaid transport rung (U1). It adds the table every later unit writes to, and
one startup tripwire that exists because the thing it guards fails silently.

## What it is

`plaid_items` — one row per linked Plaid Item — added to `backend/db/models.py`, to the
`HOUSEHOLD_SCOPED` tuple, and created by `alembic/versions/0005_plaid_items.py` with
`ENABLE` **and** `FORCE ROW LEVEL SECURITY`, a `USING`/`WITH CHECK` policy on
`app.household_id`, a grant to `cfo_app`, and a named UNIQUE on `plaid_item_id`. Adding it takes the
scoped-table count from six to seven. The migration names its own table as a **literal**, never
`from backend.db.models import HOUSEHOLD_SCOPED` — that is the whole point of ticket 0033, and 0005
is the first new scoped table since it was written.

`access_token` is stored in **plaintext**. In Sandbox that is harmless: the token grants access to
fabricated data and can drain nothing, so KMS envelope encryption defers to the first real token
(`architecture.md` [7.2]).

## The guard, and why it is not a `dek_id` check

The trigger fires quietly. The day `PLAID_ENV` flips to `production`, the same plaintext column
becomes a live credential — with no migration to force the question and no failing test to notice.
So `backend/db/session.py` grows `assert_plaid_tokens_safe_at_rest()`, wired into
`backend/main.py`'s lifespan next to `assert_rls_binds()` and `_assert_migrated()`. It refuses to
start when `PLAID_ENV != sandbox` unless envelope encryption is actually active.

The signal is deliberately the encryption **capability**, never `households.dek_id`. A `dek_id` with
no key behind it — which is every synthetic household today — is exactly the false "looks protected"
a presence-check would be fooled by: it would pass while the token stayed in the clear. So
`_plaid_token_encryption_active()` is honestly `False` until KMS lands, and a non-sandbox boot is
refused. The interim invariant, explicit until then: **nothing sets `dek_id` before the encryption
path that consumes it.**

`PLAID_ENV` unset defaults to `sandbox`, so every existing deploy is unaffected — the guard is a
no-op until someone opts into production.

## Verified against a real Postgres

- **Migration up/down/up on a fresh database from 0001.** `plaid_items` is created, dropped clean on
  downgrade (`to_regclass` returns NULL), and re-created — the fresh-from-zero case that broke 0033.
  The downgrade drops a policy and a table and touches no role and no database it never created
  (0001's and 0020's traps).
- **RLS proven, not assumed.** `plaid_items` rides the `HOUSEHOLD_SCOPED` parametrization in
  `tests/test_schema.py` and `tests/test_idor.py`. Crucially, `test_idor.py`'s `two_households`
  fixture now inserts a `plaid_items` row per household, so the leak test exercises the table with
  real rows rather than proving isolation on an empty one — the built-tested-never-exercised failure
  this repo keeps finding.
- **The guard is wired, not merely defined.** `tests/test_schema.py::TestPlaidTokenStartGuard` tests
  the function directly (sandbox boots, production refused, a non-null `dek_id` still refused), and
  `tests/test_backend_api.py` drives the real lifespan through `TestClient` and asserts a production
  `PLAID_ENV` stops the service coming up.
- Column-level: `plaid_item_id` UNIQUE across households, `status` CHECK on the three
  `ConnectionState` values, `healthy`/no-cursor/no-sync defaults.

Full suite green against Postgres 17.

## Acceptance criteria

- [x] `plaid_items` created by a migration, RLS enabled AND forced, policy present, granted to `cfo_app`.
- [x] Added to `HOUSEHOLD_SCOPED`; migration names the table literally (0033's rule).
- [x] Migration up and down are clean on a fresh database.
- [x] `access_token` start guard refuses `PLAID_ENV != sandbox` unless encryption is active — keyed
      on the encryption capability, not `dek_id` presence, including the non-null-`dek_id` case.
- [x] Guard wired into startup and proven to gate it via `TestClient`.
- [x] IDOR suite exercises `plaid_items` with real rows.

## Follow-up worth considering

- The full IDOR extension the plan's Verification Strategy calls for (repository layer AND repository
  bypassed, for both new scoped tables) completes in U5 alongside `plaid_transactions`. U1 lands the
  `plaid_items` half early rather than shipping a new scoped table unexercised.
- `_plaid_token_encryption_active()` returns `False` unconditionally until KMS lands. Whoever builds
  KMS envelope encryption flips it to a real check (client initializes AND token is ciphertext) and
  moves the comment with it.
