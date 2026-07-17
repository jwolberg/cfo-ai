---
id: "0038"
title: The Sandbox harness — the hard gate
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md
depends_on: ["0034", "0035", "0036", "0037"]
created: 2026-07-17
completed: 2026-07-17
---

# The Sandbox harness — the hard gate

U5: the one test that proves the transport against **Plaid itself**, not a mock. Every other test in
this rung fakes the Plaid client at the seam; this drives the real Sandbox API end to end.

## Status: crossed — green against real Plaid Sandbox on 2026-07-17

`test_the_transport_works_end_to_end_against_plaid_sandbox` **passed against live Sandbox**: a real
item created, exchanged, and synced; real transaction rows landed in Postgres; the cursor persisted
(an unchanged re-sync was a no-op); a fired update was consumed incrementally from the stored cursor
and converged back to a no-op; and `reset_login` flipped the item to `login_required` with freshness
frozen. This is the pattern the tickets README says every defect in this repo shared — "a mechanism
that was built, tested, and never actually exercised" — and it is now the one that *was*.

## Two things Sandbox does differently from the plan (reconciled on first run)

Both are the plan's assumptions meeting reality, exactly what a hard gate is for:

1. **`/sandbox/item/fire_webhook` refuses `SANDBOX_WEBHOOK_INVALID` unless the item has a webhook URL
   set.** The plan created the item without one. Fix: `_create_and_exchange` configures a placeholder
   webhook URL via `SandboxPublicTokenCreateRequestOptions`. (The test drives `run_sync` directly and
   never receives the webhook, so delivery is irrelevant — only that a URL exists.)
2. **`fire_webhook SYNC_UPDATES_AVAILABLE` generates a *new* batch of transactions; it does not
   redeliver.** The plan modeled it as a redelivery whose sync would be a no-op — but the real sync
   returned `(32 added, 16 modified)`. That is not a bug; it is Sandbox generating data, and the
   32/16 landing *incrementally from the stored cursor* is itself proof the cursor persisted. So the
   test now proves the two properties separately: an **unchanged re-sync** is the redelivery-no-op
   (step 2), and the **fired webhook** is the incremental-update path (step 3). Plaid corrected the
   plan.

## What it does (verified against real Sandbox)

`test_the_transport_works_end_to_end_against_plaid_sandbox`:

1. `/sandbox/public_token/create` (First Platypus Bank, `transactions`, placeholder webhook URL) →
   exchange → store the item. No Link UI — which is what lets the Link flow leave this rung unproven.
2. First `run_sync`: real rows land, the cursor persists, freshness stamps, status healthy. Then an
   **unchanged re-sync is a no-op** — the redelivery-as-no-op / resumability proof against the real
   cursor.
3. `/sandbox/item/fire_webhook` (SYNC_UPDATES_AVAILABLE) → `run_sync`: Sandbox generates a new batch,
   which is appended **incrementally from the stored cursor** (exact-delta row-count check, so a reset
   or double-count fails) → a converging re-sync returns to a no-op.
4. `/sandbox/item/reset_login` → `run_sync`: `status` flips to `login_required`, the loop stops, and
   `last_successful_sync_at` does **not** advance.

## To re-run the gate

```
export PLAID_CLIENT_ID=...        # from the Plaid dashboard; Sandbox is free and auto-approved
export PLAID_SECRET=...           # the Sandbox secret
export PLAID_ENV=sandbox
export TEST_DATABASE_URL=postgresql+psycopg://.../cfo_ai_test
.venv/bin/pytest tests/test_plaid_sandbox.py -v
```

## Acceptance criteria

- [x] `test_the_transport_works_end_to_end_against_plaid_sandbox` **passes against real Sandbox**
      (create → exchange → sync → no-op re-sync → fired incremental update → reset_login → status flip).
      Green on 2026-07-17.
- [ ] At least one `removed` transaction lands as a NULL-column row — the second test, still an
      explicit `skip` because forcing a removal in Sandbox is not deterministic from the sync flow
      alone (needs a custom Sandbox user or the `/sandbox/transactions` endpoints). Wire the exact
      mechanism next. U3 already proves the NULL-column insert at the schema layer, so the residual
      risk is "does Sandbox emit this shape", not "does our code handle it".
- [x] The harness lints and skips loudly without credentials (no fabricated green).
- [x] The `0021` IDOR suite is extended to `plaid_items` and `plaid_transactions` (done across
      U1/U3/U2: real rows in the `two_households` fixture, and the definer-function tests).

## Also owed before a *serving* rung (not this one)

- **GCP provisioning** (ADR-0005 [4]): the Cloud Tasks queue + `cloudtasks.enqueuer` grant + OIDC push
  target (U2), and the Cloud Scheduler job hitting `/plaid/sync/poll` + the `plaid_webhooks` retention
  purge (U4). None is code; all fail at first use, not at deploy.
- **The Neon migration + deploy** — `docs/runbooks/deploy.md` [2], its claims measured not trusted.
- The recurring-event detector, Link UI, KMS, normalization, `0029` — all out of this rung by design
  (the plan's Scope Boundaries).
