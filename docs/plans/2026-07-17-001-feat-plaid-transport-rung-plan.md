---
title: The Plaid transport rung — items, a doorbell, and cursored sync, ending at rows
type: feat
status: active
date: 2026-07-17
origin: docs/brainstorms/2026-07-16-plaid-the-transport-rung.md
adr: docs/decisions/0005-the-raw-webhook-table-is-unscoped.md (owed by U2)
---

# The Plaid transport rung — items, a doorbell, and cursored sync, ending at rows

## Summary

Build the plumbing that must exist before anyone links a bank: Link token exchange, item storage,
a webhook doorbell, and a cursored `/transactions/sync` loop that lands rows in a table — proven
end to end against Plaid **Sandbox**, linked **by us**, touching **nothing served**.

The ask was *"scope out Plaid integrations so test customers can add their bank info."* The
brainstorm ([`2026-07-16-plaid-the-transport-rung.md`](../brainstorms/2026-07-16-plaid-the-transport-rung.md))
split that sentence into two projects and this plan builds the lower one. Three findings shaped it:

1. **Plaid's gate is smaller than our docs claim.** As of 2026-04-15 Plaid replaced Limited
   Production with a free, auto-approved **Trial plan** carrying real production data — verified
   against Plaid's live docs on 2026-07-17. For a pilot this size *there is no review to pass*.
   What binds instead is a **10 Production Item cap that `/item/remove` does not refund**, and a
   **one-way** upgrade to full Production. Neither touches this rung: Sandbox Items do not count.
2. **The real blocker is ours and it is in none of our Plaid docs.** `derive_cash_events()`
   (`backend/precompute.py:316-419`) reads the simulator's **spec**, not history; the
   recurring-event detector does not exist. Every other derivation is already pure over `History`.
   A real household linked today assembles a snapshot with **no events, and therefore no forecast.**
   The detector is the largest unbuilt piece from bank-login to sweep — and the one thing Sandbox
   cannot help build, because Sandbox data does not reconcile ([0.4]).
3. **This rung buys engineering de-risking and zero measurement.** *"Sandbox data is not always
   based on a consistent data source across different API calls"* (Plaid's words). It proves the
   transport; it proves nothing about the world. `prd.md` §8.1's central limitation is not repaired
   by swapping our synthetic data for Plaid's.

**This plan ends at rows in a table.** The seam to `assemble_snapshot()`
(`backend/precompute.py:817-881`) is named and not crossed. At the end of it **no test customer can
link a bank and nothing in the product changes** — what it buys is that the expensive serving rung
stops being a guess.

---

## Build Progress

*Not started. Five units, five tickets (`0034`–`0038`), one ADR (`0005`). No code yet.*

| Unit | Ticket | Status |
|------|--------|--------|
| U1 `plaid_items`, RLS, and the migration | `0034` | not started |
| U2 The doorbell, the raw store, and the queue | `0035` | not started |
| U3 `plaid_transactions`, append-only | `0036` | not started |
| U4 The cursored sync loop | `0037` | not started |
| U5 The Sandbox harness | `0038` | not started |

---

## Problem Frame

### The seam this rung stops at

`assemble_snapshot()` (`backend/precompute.py:817-881`) is where `History` becomes a `Snapshot`, and
it is the correct place for real rows to enter — exactly as `0022` and `architecture.md` [2] assume.
It has one hole. `Snapshot.events` (`engine/models.py:615-696`) is what `forecast.py` projects, and
the forecast is the product. Those events come only from `derive_cash_events()`, which reads
`spec.payroll` / `spec.bills` / `card.payment` off the **simulator spec**, never from transaction
history — its own docstring says so: the events are read *"straight off the spec that generated the
household … just handed it earlier, and without the detector's errors."*

Every other derivation — `derive_card`, `classify_behavior`, `observed_monthly_payment`,
`observed_monthly_charges`, `infer_close_day`, `detect_unmatched_payments`,
`daily_discretionary_high`, `income_variation`, `spend_30d_high`
(`backend/precompute.py:439-473`, `511-753`) — is already a pure function of `History` and would run
on real rows tomorrow. So the wall between a bank login and a decision is a **single missing
function**, the recurring-event detector, and it is not on `architecture.md` [7]'s pre-migration
list. **This plan does not build it** ([Scope Boundaries](#explicitly-not-in-this-plan)); it builds
everything that feeds it and stops one seam short.

### Why Sandbox decides the rung's shape, not just its data

Our derivation *walks history to compute a balance*: `derive_card` reconstructs `unbilled_balance`
from `card_charged_between()` over history-as-of-today, then `statement_balance` from the ledger
balance minus it (`backend/precompute.py:683-684`). Against a data source whose transactions
contradict its balances — which Plaid says Sandbox is — that arithmetic produces garbage, and **you
cannot tell your bug from Plaid's inconsistency.** That is why the rung ends at rows and defers
normalization and the detector to a rung with real data behind it.

| Sandbox **can** prove | Sandbox **cannot** prove |
|---|---|
| Link, token exchange, item storage | Normalization — pending→posted needs real institution behavior |
| `/transactions/sync` cursor mechanics, resumability, redelivery as a no-op | Derivation correctness — the data does not reconcile |
| The webhook doorbell: dedup, enqueue, never work inline | The recurring-event detector |
| Item error states → `ConnectionState` → `balance_age_days` | **Anything about the world** |

---

## Resolved Decisions

Carried from the brainstorm and the answered fork; summarized so this plan reads alone.

| | Decision | Source |
|---|---|---|
| **Scope** | Transport only, against **Sandbox**, linked **by us**. Ends at rows in a table. | brainstorm [0.1], [1] |
| **The doorbell is IN** | This rung builds the webhook doorbell, not only the nightly poll. It takes on a queue (**Cloud Tasks**) and an ADR. The origin leaned toward deferring (*"smaller, keeps the dependency question shut"*); this rung overrides that because the doorbell's dedup/idempotency is *"genuinely hard to get right later,"* Sandbox can prove it now (`fire_webhook` → sync → assert-no-op **is** U5), and retrofitting it onto a live Link flow means getting idempotency right under real traffic instead of on demand. The cost paid now — one queue, one ADR — is bounded and named; the cost deferred is not. | answered 2026-07-17 (brainstorm [5.2]) |
| **The raw webhook table is unscoped; the worker reads the mapping through a `SECURITY DEFINER` function** | Plaid's webhook carries `item_id`, not `household_id`, so the doorbell cannot resolve a household before RLS is set. The raw store is **unscoped** (option 1) — mirroring `GET /households` (`backend/main.py:325-333`). But resolving `item_id → household_id` also requires *reading* `plaid_items`, which is RLS-FORCEd and returns nothing until `app.household_id` is set, and `assert_rls_binds()` forbids the `BYPASSRLS` shortcut. So the worker resolves the mapping through a narrow `SECURITY DEFINER` function returning only `household_id` for a `plaid_item_id` while `plaid_items` stays FORCEd — then `SET LOCAL`, and every financial read is scoped. Both the unscoped table and the definer function are the first holes in *"every table is scoped by `household_id`"* (`architecture.md` [4]) → **ADR-0005** covers both. | brainstorm [2] U2; review 2026-07-17 |
| **`access_token` is plaintext now, guarded by an env flag** | A Sandbox token protects nothing, so KMS defers to the first real token (`architecture.md` [7.2]). But the trigger fires quietly — the day `PLAID_ENV` flips to `production`, a plaintext column becomes a real credential. So the service **refuses to start with `PLAID_ENV != sandbox` unless a live KMS client initializes and the token column is encrypted** — keyed on a real encryption signal, *not* merely `dek_id IS NULL`, since a stray non-null `dek_id` would otherwise satisfy a presence-check while the token stayed plaintext. Same shape as `assert_rls_binds()` (`backend/db/session.py:34-68`). Interim invariant, explicit until KMS lands: **nothing sets `dek_id` before the encryption path that consumes it.** | brainstorm [5.1]; review 2026-07-17 |
| **`pending_transaction_id` is a column with no logic** | The reconciliation that consumes it is normalization — [3]'s, not this rung's. | brainstorm [2] U3 |
| **Sandbox items attach to a hand-picked `household_id`** | There is no owner column and no `users` table (`households` is `id, archetype, dek_id, created_at, deleted_at`, `backend/db/models.py:59-79`). Internal linking pins an item to a chosen household as a **fixture**, not a design. | brainstorm [5.3] |

---

## Key Technical Decisions

**The migration hardcodes its own `HOUSEHOLD_SCOPED` list.** Adding `plaid_items` (U1) and
`plaid_transactions` (U3) takes the scoped count from **six to eight**
(`backend/db/models.py:251-258`). This is exactly the operation `0033` documents as invisible: a
migration that imports the live constant and iterates it stays correct *"for exactly as long as the
constant stood still — which was every day until `0031` added a table to it."* The migration lists
its tables literally. **Read `0033` before writing it.**

**Append-only, so sync's three outcomes are all inserts.** `/transactions/sync` returns
added/modified/removed; every one is an `INSERT` carrying a `change_type`, never an `UPDATE` or
`DELETE` (`architecture.md` [4]). Corrections are new rows.

**Idempotent by cursor, resumable by construction.** The cursor is persisted per item; a redelivered
webhook re-runs `/transactions/sync` from the stored cursor and Plaid returns an empty page — the
redelivery is a no-op with no dedup logic of its own. The doorbell's dedup key
(`item_id` + `webhook_code` + cursor) protects the *enqueue*, not the sync.

**Two triggers on one item must not race.** The Cloud Tasks worker and the nightly poll can both fire
for the same item; if they read the same cursor concurrently, both fetch the same page and both
insert it. A `UNIQUE` constraint is the *wrong* guard — on an append-only table a legitimate second
`modified` of the same transaction shares `(plaid_item_id, plaid_transaction_id, change_type)` and
must be allowed. Instead **sync serializes per item**: the worker takes a `SELECT … FOR UPDATE` on
the `plaid_items` row (or a Cloud Tasks task name keyed on `item_id` so overlapping dispatches
collapse) before reading the cursor, so a concurrent run blocks rather than double-inserts.

**`last_successful_sync_at` is not bookkeeping.** It *becomes* `Account.balance_age_days`, the
freshness gate — *"the thing standing between a dropped webhook and an overdraft"* (`architecture.md`
[3.1]). On success it is stamped; on `ITEM_LOGIN_REQUIRED` the item's `status` flips and the loop
stops. `/sandbox/item/reset_login` exercises `ConnectionState`
(`HEALTHY`/`LOGIN_REQUIRED`/`DISCONNECTED`, `engine/models.py:58-61`) for the first time — today it
is a field the seeder sets to a constant.

**The doorbell never works inline.** It persists the raw payload and enqueues, full stop
(`architecture.md` [3.1]) — *"a slow handler causes retries, which cause the duplicates you are
trying to avoid."* The nightly reconciliation poll runs for **every** item regardless of webhooks,
because deliveries will be missed and *"we would rather find out in hours than when someone's rent
bounces."*

---

## High-Level Technical Design

```
Plaid Sandbox
  │  /sandbox/public_token/create  (no Link UI — U5)
  ▼
POST /plaid/link/exchange ── public_token → access_token, item_id ──► plaid_items   U1
                                                                       (RLS-scoped)
  Plaid webhook ──► POST /plaid/webhook ──► plaid_webhooks (UNSCOPED)  ──► Cloud Tasks   U2
                    (doorbell: verify JWT, persist, enqueue — never inline)   │
                                                                            ▼
  nightly poll ─────────────────────────────► sync worker ──────────────────┤   U4
                          resolve household (SECURITY DEFINER fn), lock item, SET LOCAL │
                                               /transactions/sync per item    │
                                                     │                        │
                                        added/modified/removed → INSERT       │
                                                     ▼                        │
                                          plaid_transactions (append-only)    │   U3
                                          stamp last_successful_sync_at ◄──────┘
                                                     ┆
                                          ┅┅┅ SEAM: assemble_snapshot() ┅┅┅  (NOT crossed)
```

### The schema

```sql
plaid_items (
  id, household_id FK, plaid_item_id UNIQUE, institution_id,
  access_token,                 -- plaintext in Sandbox; env-flag guarded (see Resolved Decisions)
  cursor,                       -- /transactions/sync position, persisted
  status,                       -- ConnectionState: healthy | login_required | disconnected
  last_successful_sync_at,      -- becomes Account.balance_age_days
  error_code, created_at
)                                                         -- HOUSEHOLD_SCOPED, RLS FORCEd

plaid_webhooks (
  id, plaid_item_id, webhook_type, webhook_code, cursor,
  payload JSONB, received_at,
  UNIQUE (plaid_item_id, webhook_code, cursor)            -- natural dedup key
)                                                         -- UNSCOPED (ADR-0005)

plaid_transactions (
  id, household_id FK, plaid_item_id, plaid_account_id,
  plaid_transaction_id, pending_transaction_id NULL,      -- column only; no reconciliation
  amount NUMERIC NULL, date NULL, name NULL, merchant_name NULL,  -- NULL on 'removed': Plaid sends only ids
  change_type,                  -- added | modified | removed  (all INSERTs)
  ingested_at
)                                                         -- HOUSEHOLD_SCOPED, RLS FORCEd, append-only
```

`plaid_items` and `plaid_transactions` follow `0001`/`0004` exactly — `ENABLE` **and** `FORCE ROW
LEVEL SECURITY`, a `USING`/`WITH CHECK` policy on `current_setting('app.household_id', true)`, a
grant to `cfo_app`, and membership in `HOUSEHOLD_SCOPED`. `plaid_webhooks` is deliberately outside
it. The worker's `item_id → household_id` lookup goes through a `SECURITY DEFINER` function
(`plaid_household_for_item(text) RETURNS uuid`) that runs as the table owner and returns only the
household id — the single sanctioned way to read `plaid_items` before a scope is set (ADR-0005).
`removed` sync events carry only ids, so `amount`/`date`/`name`/`merchant_name` are nullable rather
than defaulted to a fabricated value.

---

## Implementation Units

### U1. `plaid_items`, RLS, and the migration — `0034`

The table above; RLS FORCEd; added to `HOUSEHOLD_SCOPED` with a **hardcoded** list in the migration.
The env-flag start guard for `access_token` lands here, alongside `dek_id`
(`backend/db/models.py:67-74`, whose comment already says the KMS wiring *"lands with Plaid"*).

**Files:** `backend/db/models.py`, `backend/db/session.py` (the start guard), `alembic/`,
`tests/test_schema.py`

**Verification:**
- Migration up/down clean; the down does not name a database it never heard of (`0020`'s trap).
- The start guard refuses `PLAID_ENV != sandbox` unless a live KMS client initializes and the token
  column is encrypted (not merely `dek_id` non-null) — tested the way `assert_rls_binds()` is,
  including the case where `dek_id` is set but no encryption path exists (must still refuse).
- **Neon must be migrated before this deploys** — `_assert_migrated()` (`backend/main.py`) refuses to
  start otherwise, the same trap that left the deploy stale for eleven tickets.
  `docs/runbooks/deploy.md` [2] is the step; **measure its claims, do not follow them**
  ([memory](../../CLAUDE.md): 6/6 runbook claims were false last time).

### U2. The doorbell, the raw store, and the queue — `0035`

`POST /plaid/webhook` **verifies Plaid's `Plaid-Verification` JWT** (fetch and cache the key via
`/webhook_verification_key/get`, validate the signature and body hash) and *then* persists the raw
payload under the dedup key and enqueues onto **Cloud Tasks** — *never* inline. An unsigned or forged
payload is rejected before it touches the table or the queue. `POST /plaid/link/exchange` turns a
`public_token` into an item row; it carries `Depends(require_api_key)` like every other route, and
`household_id` is **fixture-supplied by the internal caller** ([5.3]), never read from the request
body. The raw store is **unscoped**; the doorbell only writes the raw row and enqueues — the worker
that resolves the household and `SET LOCAL`s lives in **U4** (see there).

This is this backend's **first write path.** `POST /assistant/message` (`backend/main.py:522`) is the
only non-GET route today and it writes nothing — which is also why `0016` never built the attestation
action (*"attesting is a write, and this backend has no database by design"*, since superseded by
`0004`). Link is the write path that unblocks it; **whoever builds Link should expect to close
`0016`** — flagged in [3], not this rung.

**New dependency — Cloud Tasks, justified:** the doorbell must hand off work and this stack has no
queue. `architecture.md` [1.2] cut Temporal (*"adopt it when money moves"*) and Redis (*"no
cache-shaped access pattern"*); neither argument is about a webhook fan-out. Cloud Tasks is the
GCP-local answer and the rest of the stack is already Cloud Run.

**ADR-0005 — the two tenancy exceptions.** Records *both* holes the doorbell opens: (a) the unscoped
`plaid_webhooks` table (the webhook *write* — `item_id` before a household is known), and (b) the
`SECURITY DEFINER` `plaid_household_for_item()` function the worker uses to *read* the mapping out of
RLS-FORCEd `plaid_items` without a scope set. Names why option 1 (unscoped raw table) beats the
narrow-lookup and worker-only alternatives, why the definer function is preferred over granting the
app role `BYPASSRLS`, and — since the table accumulates third-party payloads — a **retention policy**
(purge consumed rows older than N days) so the unscoped table does not become an unscoped archive.

**Provisioning (mirrors `deploy.md`'s Neon/Secret-Manager rigor).** No startup gate
(`assert_rls_binds`, `_assert_migrated`) checks for the queue, so a missing one fails at the first
webhook, not at deploy. U2 names the steps: create the Cloud Tasks queue, grant the service account
`cloudtasks.enqueuer`, and require an **OIDC token on the push target** so only Cloud Tasks can
invoke the worker route. The nightly-poll trigger is provisioned in U4.

**Files:** `backend/plaid/webhook.py` (JWT verification), `backend/plaid/link.py`,
`backend/plaid/tasks.py` (enqueue + OIDC push target), `backend/main.py`, `backend/db/models.py`
(`plaid_webhooks`), `pyproject.toml` **and `backend/requirements.txt`** (`google-cloud-tasks`,
`plaid-python`), `tests/test_requirements.py` (register `plaid`/`google` in `DISTRIBUTION_OF` — an
unknown root is silently skipped, the PR #50 mode `deploy.md` documents), `docs/decisions/0005-*.md`,
`tests/test_webhook.py`

**Verification:**
- An unsigned or forged webhook is rejected before any persist or enqueue (post a payload with a
  bad/absent `Plaid-Verification` JWT; assert a 4xx and an empty table).
- A duplicate webhook (same `item_id`+`code`+cursor) is dropped by the unique constraint, not by
  application logic.
- The handler enqueues and returns without a Plaid call (asserted — no outbound HTTP in the request
  path).
- `/plaid/link/exchange` requires the API key and ignores a caller-supplied `household_id`.
- The manifest test fails if `plaid`/`google` are in `pyproject.toml` but absent from
  `backend/requirements.txt`. (The worker's scoping is verified in U4.)

### U3. `plaid_transactions`, append-only — `0036`

The table above; added/modified/removed all `INSERT`; `pending_transaction_id` a column with no
consumer. **This is the table `spend_projections` was waiting for** — `backend/db/models.py:215` says
*"The one table here that ingest deletes. Ticket 0031."* It does **not** die in this rung (the seam
is not crossed); the plan records that its deletion belongs to the serving rung, not to a table left
annotated for a deletion nobody scheduled.

**Files:** `backend/db/models.py`, `alembic/`, `tests/test_schema.py`

**Verification:**
- A `modified` and a `removed` each land as a new row with the right `change_type`; nothing is
  updated or deleted in place.
- A **Plaid-shaped `removed` payload** (only `transaction_id`/`account_id`, no `amount`/`date`/`name`)
  inserts cleanly with those columns NULL — not a fabricated full row.
- A `NUMERIC` amount round-trips a `Decimal` exactly.

### U4. The sync worker and the cursored loop — `0037`

**U4 owns the worker end to end:** it dequeues the Cloud Tasks job, resolves `plaid_item_id →
household_id` via the `SECURITY DEFINER` function, `SET LOCAL app.household_id`, then runs
`/transactions/sync` per item — cursor persisted, redelivery a no-op by construction. (U2's doorbell
only writes the raw row and enqueues; the scoping step lives here, with `repository.py`.) The same
loop has a second trigger — a **nightly reconciliation poll for every item regardless of webhooks** —
which needs no queue and **depends only on U1+U3**. Before reading the cursor the worker takes a
`SELECT … FOR UPDATE` on the item row so the two triggers cannot race into a double-insert. On
success, stamp `last_successful_sync_at`; on `ITEM_LOGIN_REQUIRED`, set `status` and stop.

**Provisioning:** the nightly poll is driven by **Cloud Scheduler** hitting an internal endpoint —
named here because, like the queue, nothing checks for it at startup and a missing schedule fails
silently at the first missed night.

**Files:** `backend/plaid/sync.py`, `backend/plaid/client.py`, `backend/db/repository.py` (scoped
writes + the definer-function call), `tests/test_sync.py`

**Verification:**
- The worker resolves the household and `SET LOCAL`s before any scoped read; a job for item→household
  B never touches A's rows.
- Two concurrent runs on one item do not double-insert (the `FOR UPDATE` serializes them).
- A second sync from the stored cursor returns empty and writes nothing — resumability and
  redelivery-as-no-op in one test.
- An interrupted sync resumes from the persisted cursor, not from the start.
- `ITEM_LOGIN_REQUIRED` flips `status` and halts the loop; `last_successful_sync_at` is untouched on
  failure and advanced on success.

### U5. The Sandbox harness — `0038`

The unit that makes the other four real, and the hard gate:
`/sandbox/public_token/create` → exchange → sync → `/sandbox/item/fire_webhook` → sync again →
**assert the redelivery changed nothing** → `/sandbox/item/reset_login` → **assert `status` becomes
`login_required` and the loop stops.** Credentials `user_good` / `pass_good`. Because
`/sandbox/public_token/create` bypasses Link entirely, **U5 needs no UI** — which is what lets the
Link flow leave this rung without leaving it unproven.

**Files:** `tests/test_plaid_sandbox.py`, a small harness under `backend/plaid/` if needed

**Verification — this *is* the verification:** a real Sandbox run, not a mocked one, and it must
include at least one `removed` transaction landing as a NULL-column row (U3's sparse-payload case),
not only added/modified. See [Verification Strategy](#verification-strategy).

### Dependency order

```
U1 (plaid_items) ──┬── U2 (doorbell + queue) ───────────────┐
                   ├── U3 (transactions) ──┐                 │
                   │      (poll path: U1+U3)├── U4 sync loop ─┤── U5 (Sandbox harness)
                   └────────────────────────┘  + nightly poll │
                       worker/webhook path also needs U2's queue
```

U2 and U3 are independent given U1. **U4 splits by trigger:** its core sync loop and the nightly poll
depend only on U1+U3 and can build in parallel with U2; only the Cloud Tasks-triggered worker path
also needs U2's queue. U5 exercises all four.

---

## Scope Boundaries

### Explicitly NOT in this plan

Every trigger is an **event, not a date** — `architecture.md` [4.1]'s rule.

| Not built | Trigger |
|---|---|
| **The recurring-event detector** | Serving a real household. The gating piece; needs real data to build against; its own brainstorm. |
| **Link UI + the served write path** | A person other than us linking. Closes `0016` on the way through. |
| **Clerk / identity** | The first **real** token. Internal-only licenses the deferral, and only while the data is Sandbox. |
| **KMS envelope encryption** | The first real token. `dek_id` is a column with no key; the env-flag guard (U1) is the interim. |
| **Normalization** (pending→posted, internal transfers) | Real data. Sandbox cannot validate it; getting it wrong is one-directional — double-counting under-sweeps, dropping overdrafts. |
| **GLBA posture** | The first real third party's data. Internal-only defers the legal artifact, not the engineering. |
| **Any wiring into `assemble_snapshot()`, `accounts`, `cards`, or the served surface** | The serving rung. The seam is named and not crossed. |

### Deferred to follow-up

- The **10 Production Item cap** and the one-way Trial→Production upgrade. Real only at Production
  scale; Sandbox Items do not count. Belongs to whoever flips `PLAID_ENV`.
- **`0029`** — the income-bucket gate that refuses ~half of realistic households on a calendar
  artifact. Does not touch transport; must be settled before anyone promises a **serving** rung, or
  the honest promise is *"link your bank and be refused about half the time, for a reason that is our
  bug."*

---

## Risks

| Risk | Mitigation |
|---|---|
| **The migration adds a table to `HOUSEHOLD_SCOPED` by importing the live constant** — invisible until the next table shifts it (`0033`). | The migration hardcodes its table list. U1's verification includes a fresh-database migration, the only case that broke. |
| **`PLAID_ENV` flips to `production` and a plaintext `access_token` becomes a real credential** with no migration and no failing test (brainstorm [5.1]). | The env-flag start guard (U1) refuses to boot unless KMS is live and the token is encrypted — keyed on a real encryption signal, not a non-null `dek_id`, so a stray placeholder cannot satisfy it. |
| **The unscoped raw table is a real hole in tenancy** and invites more. | ADR-0005 bounds it to `plaid_webhooks` plus the definer function; the worker scopes immediately; every financial row stays RLS-FORCEd; the IDOR suite covers the scoped tables. |
| **The public webhook endpoint accepts forged payloads**, filling tables, driving enqueues, and forcing real syncs on a household's token. | U2 verifies Plaid's `Plaid-Verification` JWT before persist/enqueue, and an OIDC token gates the Cloud Tasks push target. |
| **Two sync triggers race into duplicate transaction rows** (worker + nightly poll on one item). | The worker takes `SELECT … FOR UPDATE` on the item before reading the cursor; a `UNIQUE` constraint is deliberately *not* used — it would block legitimate repeated `modified` rows. |
| **A new dependency passes CI but fails the Cloud Run buildpack at import** (PR #50's mode). | U2 adds `backend/requirements.txt` and registers `plaid`/`google` in `test_requirements.py`'s `DISTRIBUTION_OF`, which otherwise silently skips unknown roots. |
| **Sandbox data does not reconcile**, so a green derivation against it would be meaningless. | The rung stops at rows. Derivation and normalization are explicitly out; nothing here validates arithmetic against Sandbox balances. |
| **`_assert_migrated()` refuses to start against an unmigrated Neon** (the eleven-ticket stall). | Neon migrated before deploy; `deploy.md` [2] is the step, its claims measured not trusted. |
| **A green test suite is mistaken for evidence** — every defect `0019`–`0033` had one. | U5 is a real Sandbox run, not a mock. The IDOR suite is extended to the two new scoped tables. See below. |

---

## Verification Strategy

Per unit, above. Across the plan:

- **U5 is a hard gate.** A Sandbox run that creates an item, syncs, redelivers, and breaks the login
  — not a mocked one. It is the reason to build the other four. The tickets README names the pattern
  every defect in this repo has shared — *"a mechanism that was built, tested, and never actually
  exercised … None of them had a symptom. All of them had a green test"* (the README enumerates six
  such defects). An ingest path with unit tests and no Sandbox run would be the next, and the most
  expensive, because the thing it would be wrong about is someone's bank.
- **The `0021` IDOR suite, extended to `plaid_items` and `plaid_transactions`.** RLS on a new scoped
  table is precisely the mechanism that has been built-tested-and-never-exercised twice already.
  Asserted at the repository layer **and** with the repository bypassed, so RLS is proven
  independently.
- **The existing gate** — `pytest`, `ruff`, the mobile checks (`.github/workflows/`).

**A green test suite is not evidence here.**

---

## Review History

**2026-07-17 — `/ce-doc-review` (coherence, feasibility, product, security, scope, adversarial).**
12 findings actioned. The review's central catches, all now folded in above: the worker could not
read RLS-FORCEd `plaid_items` to resolve a household (P0 — the design as first written could not
execute; fixed with a `SECURITY DEFINER` function), the webhook endpoint had no Plaid signature
verification, the new deps skipped `test_requirements.py`'s manifest check (PR #50's import-failure
mode), the two sync triggers could race into duplicate rows (fixed with a per-item lock, *not* a
`UNIQUE` constraint — that would block legitimate `modified` rows), and the env-flag guard tested
`dek_id` presence rather than real encryption. Five FYI observations were left as notes for ADR-0005
(retention/TTL, task-payload-as-ref, webhook-payload-shape verification, worker/commit race handling,
and whether `link/exchange` should split from U2).
