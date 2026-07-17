---
id: "0035"
title: The webhook doorbell, the raw store, and the Cloud Tasks queue
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md
depends_on: ["0034"]
created: 2026-07-17
completed: 2026-07-17
---

# The webhook doorbell, the raw store, and the Cloud Tasks queue

U2: the public endpoint Plaid rings, the unscoped table it lands in, the queue it hands work to, and
the Link exchange that creates an Item. This backend's **first write path from the outside**.

## What it is

- **`POST /plaid/webhook`** (`backend/plaid/webhook.py`) — the doorbell. Verifies the
  `Plaid-Verification` JWT (ES256; fetch the JWK by `kid` via `/webhook_verification_key/get`, check
  the signature, reject an `iat` older than 5 minutes, and confirm `request_body_sha256` matches
  *this* body), then persists the raw payload under the dedup key and enqueues a sync — **never
  inline**. A redelivery conflicts on the dedup key, inserts nothing, and enqueues nothing.
- **`POST /plaid/link/exchange`** (`backend/plaid/link.py`) — `public_token → access_token, item_id`,
  written to `plaid_items` through the repository so RLS `WITH CHECK` binds it. Carries
  `Depends(require_api_key)` like every other internal route.
- **`plaid_webhooks`** — the unscoped raw store (migration `0007`), `UNIQUE NULLS NOT DISTINCT` on
  `(plaid_item_id, webhook_code, cursor)`.
- **`plaid_household_for_item(text) RETURNS text`** — the `SECURITY DEFINER` lookup the worker (0037)
  uses to resolve `item_id → household_id` out of FORCE'd `plaid_items` before it can set a scope.
- **Cloud Tasks enqueue** (`backend/plaid/tasks.py`) with an OIDC push target.
- **ADR-0005** — records both tenancy exceptions, the retention policy, and the provisioning.
- Deps: `plaid-python`, `google-cloud-tasks`, `PyJWT[crypto]` in `pyproject.toml` **and**
  `backend/requirements.txt`, registered in `test_requirements.py`'s `DISTRIBUTION_OF`.

## The two things a review must not miss

1. **The definer function was the P0.** As first sketched, the worker could not read the mapping it
   needs: `plaid_items` is FORCE'd, so an unscoped session sees nothing, and granting the app role
   BYPASSRLS would unscope everything. The `SECURITY DEFINER` function bypasses RLS for exactly one
   lookup. Proven in `tests/test_idor.py`: an unscoped `cfo_app` session reads **zero** rows from
   `plaid_items` directly and yet resolves the household through the function.
2. **The dedup key needed `NULLS NOT DISTINCT`.** The plan's literal `UNIQUE(item_id, code, cursor)`
   would *not* dedup the SYNC_UPDATES_AVAILABLE webhook, whose cursor is NULL — Postgres treats NULLs
   as distinct by default, so two redeliveries would both insert. `NULLS NOT DISTINCT` fixes it;
   proven at the SQL layer and by `test_webhook.py`'s redelivery test.

## Verified

- **The door stays shut**: no signature, a forged signature, a valid signature over a *different*
  body, and a stale signature are each rejected 401 before any persist or enqueue (`test_webhook.py`).
- **A genuine webhook** persists once, enqueues once, and makes **no Plaid call in the request path**
  (the fake client asserts `transactions_sync` is never touched). A redelivery is deduped and does
  not re-enqueue.
- **Link exchange** requires the key and writes an item scoped to the named household (`test_link.py`).
- Migration `0007` up/down/up clean on a fresh DB; the definer bypass and the dedup verified against
  Postgres directly.
- Full suite: 562 passed, 1 skipped, on Postgres 17. `ruff` clean.

## Acceptance criteria

- [x] `POST /plaid/webhook` verifies the Plaid JWT (signature + body hash + freshness) before persist/enqueue.
- [x] Raw payload persisted to the **unscoped** `plaid_webhooks`; dedup by `NULLS NOT DISTINCT` key.
- [x] The doorbell never calls Plaid or syncs inline; it enqueues to Cloud Tasks (OIDC push target).
- [x] `POST /plaid/link/exchange` requires the API key and writes `plaid_items` through the repository.
- [x] `plaid_household_for_item()` SECURITY DEFINER function created and proven to bypass FORCE'd RLS.
- [x] ADR-0005 records both exceptions, the retention policy, and provisioning.
- [x] `plaid`/`google`/`jwt` in `DISTRIBUTION_OF`; deps in both manifests; `test_requirements.py` green.

## Follow-up / not in this ticket

- **GCP provisioning is not code and is not done here**: create the queue, grant `cloudtasks.enqueuer`,
  wire the OIDC push target, and (U4) the Cloud Scheduler job. ADR-0005 [4] lists the steps; the
  service fails at the first webhook, not at deploy, if they are missing.
- **The retention purge job** lands with the Cloud Scheduler in U4 (the grant already includes DELETE).
- `client.py` and `repository.add_plaid_item` were created here though the plan listed `client.py`
  under U4 — the Link exchange needs both. U4 extends them for the sync loop.
- Closing `0016` (the attestation write) is unblocked by this first write path but is not this rung.
