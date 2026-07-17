---
id: "0037"
title: The sync worker and the cursored /transactions/sync loop
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md
depends_on: ["0034", "0035", "0036"]
created: 2026-07-17
completed: 2026-07-17
---

# The sync worker and the cursored /transactions/sync loop

U4: the worker that lands real rows. Two triggers, one loop, ending one seam short of
`assemble_snapshot()`.

## What it is

`run_sync(engine, plaid_item_id, client)` (`backend/plaid/sync.py`) syncs one item end to end:

1. **Resolves the household** through the `SECURITY DEFINER` `plaid_household_for_item()` (the one
   read of FORCE'd `plaid_items` allowed before a scope is set, ADR-0005), then `SET LOCAL`.
2. **Takes `SELECT ... FOR UPDATE` on the item row** before reading the cursor.
3. **Loops `/transactions/sync`** from the stored cursor, appending every added/modified/removed as a
   new row via `repository.add_plaid_transaction`, until `has_more` is false. On success it advances
   the cursor and stamps `last_successful_sync_at`.
4. **On `ITEM_LOGIN_REQUIRED`** it rolls the sync back (no partial rows, cursor unmoved) and flips
   `status` to `login_required` in a separate transaction — freshness untouched, so the gate ages.

Two triggers reach it, both behind an OIDC gate (`backend/plaid/oidc.py`):

- `POST /plaid/sync/worker` — the Cloud Tasks target (a webhook fired, U2 enqueued it).
- `POST /plaid/sync/poll` — `run_poll`, the nightly Cloud Scheduler reconciliation over **every**
  item for **every** household, regardless of webhooks. It enumerates households (unscoped —
  `households` has no RLS, it is the tenant registry), lists each one's items under scope, and hands
  each to `run_sync`.

## The three things that make it correct

- **The per-item lock stops the race.** The Cloud Tasks worker and the nightly poll can fire for the
  same item; without serialization both read the same cursor and both insert the same page. A UNIQUE
  constraint is the *wrong* guard (it would reject a legitimate repeated `modified`). The
  `FOR UPDATE` on the item row makes the second run block until the first commits, then read the
  advanced cursor and find nothing. Proven with a real two-thread concurrency test: two runs on one
  item leave **2 rows, not 4**.
- **Money never touches a float.** Plaid's `amount` is a float; the worker converts it
  `Decimal(str(amount))`, never `Decimal(float)`, so the cent that reaches the NUMERIC column is the
  cent Plaid sent (ADR-0002 [2.2]).
- **Failure is atomic.** A mid-loop error rolls the whole scoped transaction back, so the cursor
  never advances past uncommitted rows and no partial page survives — verified on the
  `ITEM_LOGIN_REQUIRED` path.

## Verified (real Postgres, fake Plaid client)

- First sync appends every outcome, the `removed` row lands with a NULL amount, the Decimal
  round-trips, the cursor persists, and freshness stamps.
- It pages until `has_more` is false; a resync from the stored cursor is a no-op (resumability +
  redelivery in one).
- `ITEM_LOGIN_REQUIRED` flips status, leaves the cursor and freshness untouched, and writes no rows.
- A sync lands only in the item's own household (scoping); an unknown item is a no-op.
- The worker route refuses a request with no OIDC token and runs the sync with a permitted one.
- Full suite: 571 passed, 1 skipped, on Postgres 17. `ruff` clean.

## Acceptance criteria

- [x] Worker resolves the household and `SET LOCAL`s before any scoped read; a job for B never touches A.
- [x] Two concurrent runs on one item do not double-insert (proven, not asserted — real threads).
- [x] A second sync from the stored cursor is empty; an interrupted sync resumes from the cursor.
- [x] `ITEM_LOGIN_REQUIRED` flips `status` and halts; `last_successful_sync_at` untouched on failure.
- [x] Nightly poll runs for every item regardless of webhooks, depending only on U1+U3 for its path.
- [x] Both triggers gated by an OIDC token on the push target.

## Follow-up / not in this ticket

- **U5 is the hard gate and it is not this ticket**: a real Sandbox run (public_token → exchange →
  sync → fire_webhook → resync no-op → reset_login → status flip). It needs live Plaid Sandbox
  credentials (`PLAID_CLIENT_ID`/`PLAID_SECRET`), which are not present in this environment.
- **The nightly Cloud Scheduler job and the ADR-0005 retention purge** are provisioning, not code:
  a schedule hitting `/plaid/sync/poll` with an OIDC token, and a purge of `plaid_webhooks` rows
  older than N days. Named in ADR-0005 [4]; a missing schedule fails silently at the first missed
  night, which is why it is named rather than gated.
- `client.py` gained nothing new here — U2 already built the factory the loop uses.
