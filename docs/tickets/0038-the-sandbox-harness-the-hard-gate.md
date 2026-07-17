---
id: "0038"
title: The Sandbox harness — the hard gate
type: feat
status: blocked
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md
depends_on: ["0034", "0035", "0036", "0037"]
created: 2026-07-17
completed:
---

# The Sandbox harness — the hard gate

U5: the one test that proves the transport against **Plaid itself**, not a mock. Every other test in
this rung fakes the Plaid client at the seam; this drives the real Sandbox API end to end.

## Status: written, not yet run

`status: blocked` and it is honest. The harness (`tests/test_plaid_sandbox.py`) is complete, lints,
collects, and **skips loudly** without credentials — but it has **never been run against Plaid**,
because this build environment had no Sandbox credentials. That is exactly the state this rung's
whole philosophy refuses to paper over: the tickets README names the pattern every defect in this
repo has shared — "a mechanism that was built, tested, and never actually exercised ... All of them
had a green test." An ingest path with a green fake-client suite and no Sandbox run would be the
next, and the most expensive, because the thing it would be wrong about is someone's bank.

**So the hard gate is not yet crossed.** It is one credential away.

## What it does (once it can run)

`test_the_transport_works_end_to_end_against_plaid_sandbox`:

1. `/sandbox/public_token/create` (First Platypus Bank, `transactions`) → exchange → store the item.
   No Link UI — which is what lets the Link flow leave this rung without leaving it unproven.
2. First `run_sync`: assert real rows land, the cursor persists, freshness stamps, status healthy.
3. `/sandbox/item/fire_webhook` → `run_sync` again: assert the redelivery changed **nothing** (0/0/0)
   — resumability and redelivery-as-no-op against the real cursor.
4. `/sandbox/item/reset_login` → `run_sync`: assert `status` flips to `login_required`, the loop
   stops, and `last_successful_sync_at` does **not** advance.

## To cross the gate

```
export PLAID_CLIENT_ID=...        # from the Plaid dashboard; Sandbox is free and auto-approved
export PLAID_SECRET=...           # the Sandbox secret
export PLAID_ENV=sandbox
export TEST_DATABASE_URL=postgresql+psycopg://.../cfo_ai_test
pytest tests/test_plaid_sandbox.py -v
```

## Acceptance criteria

- [ ] `test_the_transport_works_end_to_end_against_plaid_sandbox` **passes against real Sandbox**
      (create → exchange → sync → fire_webhook → resync no-op → reset_login → status flip).
- [ ] At least one `removed` transaction lands as a NULL-column row — the second test, currently an
      explicit `skip` because forcing a removal in Sandbox is not deterministic from the sync flow
      alone (needs a custom Sandbox user or the `/sandbox/transactions` endpoints). Wire the exact
      mechanism on first run. U3 already proves the NULL-column insert at the schema layer.
- [x] The harness is written, lints, and skips loudly without credentials (no fabricated green).
- [x] The `0021` IDOR suite is extended to `plaid_items` and `plaid_transactions` (done across
      U1/U3/U2: real rows in the `two_households` fixture, and the definer-function tests).

## Also owed before a *serving* rung (not this one)

- **GCP provisioning** (ADR-0005 [4]): the Cloud Tasks queue + `cloudtasks.enqueuer` grant + OIDC push
  target (U2), and the Cloud Scheduler job hitting `/plaid/sync/poll` + the `plaid_webhooks` retention
  purge (U4). None is code; all fail at first use, not at deploy.
- **The Neon migration + deploy** — `docs/runbooks/deploy.md` [2], its claims measured not trusted.
- The recurring-event detector, Link UI, KMS, normalization, `0029` — all out of this rung by design
  (the plan's Scope Boundaries).
