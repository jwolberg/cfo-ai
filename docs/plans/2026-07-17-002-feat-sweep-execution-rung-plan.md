---
title: The sweep-execution rung — pay the card, two legs, behind a port, moving nothing first
type: feat
status: completed
date: 2026-07-17
origin: docs/brainstorms/2026-07-17-the-sweep-execution-rung.md
adr: docs/decisions/0006-*.md (owed by U4 — the FBO/custody commitment + the transfer-webhook tenancy exception)
---

# The sweep-execution rung — pay the card, two legs, behind a port, moving nothing first

## Summary

Build the write half of the sweep: the machinery that debits a customer's checking and pays down
their credit card. The engine already emits a `Decision` carrying `target_debt_id`
(`engine/models.py`, the frozen `Decision` dataclass) and **nothing consumes it** — this rung is the
consumer. Paying a card is **two legs, not one**: a **debit leg** (ACH pull from checking into a
platform funding account we hold) and a **payoff leg** (land it on the card at any issuer). Both sit
behind a `TransferProvider` port; a `transfers` ledger records the state machine; the engine's
`SWEEP_IN_FLIGHT` socket gets energized; and a real-sandbox hard gate proves it end to end.

**The whole rung ships in shadow mode: `submit()` is a logged no-op and no production money moves.**
It ends at a settled card payment in **Method's** sandbox (with a forced reversal), a funded platform
account in **Increase's** sandbox, and a shadow-mode ledger against real households. What it buys is
the one number the company is graded on before a dollar is at risk — the measured tail-risk of the
decisions it *would* have executed.

Four decisions, three from research on 2026-07-17, shaped this plan away from the brainstorm's
first draft:

1. **Saga substrate: Cloud Tasks now, Temporal later.** `architecture.md` [5] commits to Temporal
   *"when money moves"* — and this rung moves none. The repo has no Temporal; the just-merged Plaid
   rung already proved a Cloud Tasks + `SELECT … FOR UPDATE` durable pattern (`backend/plaid/sync.py`
   on `main`). The saga is built on that pattern, and **Temporal is introduced at the same trigger
   that turns `submit()` on** — not in this rung.
2. **Debit provider: Increase, funded via Plaid Auth numbers — not a processor token.** Research
   found **Increase is not a Plaid processor partner** ([plaid.com/docs/auth/partnerships](https://plaid.com/docs/auth/partnerships/)),
   so the brainstorm's "Plaid Auth → processor token → provider" handoff does **not** apply to
   Increase. The debit leg uses Plaid **Auth** to retrieve the account/routing numbers and passes them
   to Increase directly (or via an Increase External Account). The processor-token path only holds for
   Dwolla; it is dropped here.
3. **The port's shape is asymmetric across the two providers**, and the brainstorm's uniform
   `authorize/submit/status/handle_return` needs refinement (see Key Technical Decisions): `authorize`
   is a **client-side** step (pre-flight re-check + idempotency-key derivation), not a vendor call for
   either provider; `status` is a single field for Method but a **join** for Increase (which has no
   `settled` status); and returns are represented differently per vendor.
4. **Two live predecessors, both confirmed by research.** This branch predates the merge of the Plaid
   transport rung into `main`, so `backend/plaid/` is not in the tree — the rung must **merge `main`**
   first. And the `0016` attestation gate has **no backend write-path** (`derive_portfolio(...,
   attested)` in `backend/precompute.py` hardcodes `attested=True`), so an unattested population makes
   a shadow run measure nothing — closed here by **attesting pilot households by fixture**, with the
   real write-path named as a follow-up.

---

## Build Progress

*Built in shadow, 2026-07-17. Six units, six tickets (`0039`–`0044`), one ADR (`0006`), migrations
`0008`/`0009`. **Shadow-complete: no production money moves.** The full suite is green; U6's real-
sandbox hard gate skips loudly pending credentials and must run green before `submit()` turns on.*

> **Superseded on the debit rail (2026-07-18, [ADR-0007](../decisions/0007-the-debit-rail-and-the-timing-model.md), migration `0010`, ticket `0045`).**
> The body below documents the Increase-debit + Method-payoff decision as originally built. The debit
> rail is now **Plaid Transfer** (it debits the already-linked Plaid item; Increase stays a swappable
> backup, and its adapter remains in the tree), and the leg timing is settled: **wait-for-clear,
> initiated early**, with a Signal-scored prefund as a bounded exception (`saga.run_sweep`). Read the
> Increase-specific mechanics below as the *pattern* the Plaid Transfer adapter follows, not the
> current vendor.

| Unit | Ticket | Commit | Status |
|------|--------|--------|--------|
| U1 The `transfers` ledger, RLS, and the migration (`HOUSEHOLD_SCOPED` 8→9) | `0039` | `f3a235a` | shipped |
| U2 The `TransferProvider` port + `ShadowProvider` (`submit` = no-op) | `0040` | `b068abe` | shipped |
| U3 Debit-leg auth (Plaid Auth → Increase), Method Connect, the FBO funding account + credential guards | `0041` | `a85f46a` | shipped |
| U4 The durable saga on Cloud Tasks: authorize → submit → poll/webhook → settle/return | `0042` | `79c01ef` | shipped |
| U5 The `SWEEP_IN_FLIGHT` engine feedback | `0043` | `4dfb6d3` | shipped |
| U6 The provider-sandbox hard gate (Increase + Method), IDOR + idempotency assertions | `0044` | `8a4534c` | shipped (skips pending creds) |

**Carried out of scope, as the plan set (all live-money-gated):** the FastAPI webhook receiver routes +
Cloud Tasks worker + OIDC gating (mirror `backend/plaid/`; exercised with U6's live sandbox); the
real Increase/Method HTTP clients wired to production; the live-request snapshot-assembly path that
computes and passes `sweeps_in_flight` (readpath serves precomputed today); the `0016` attestation
write-path; turning `submit()` on and the Reg E/GLBA/MTL build behind it.

---

## Problem Frame

### The seam this rung starts from, and the socket it fills

The engine produces a `Decision` with `action`, `amount`, and `target_debt_id`
(`engine/models.py`, the `Decision` dataclass), persisted in the partitioned `decisions` table
(`alembic/versions/0001_household_scoped_schema.py`). Today nothing downstream reads it to move money.
`decision-engine.md` [6.4] and `architecture.md` [5] both carve this out as *"the other half of the
engineering problem."* This rung is that half.

The engine also already **models the write half's feedback**: `Snapshot.sweeps_in_flight`
(`engine/models.py`) and `ReasonCode.SWEEP_IN_FLIGHT` / `BELOW_MIN_SWEEP`, consumed in
`engine/decide.py` — but `assemble_snapshot()` (`backend/precompute.py`) hardcodes
`sweeps_in_flight=ZERO`, so the state is unreachable in production. U5 is what energizes it: an
in-flight transfer must suppress the next sweep, because *"stacking is how you overdraft someone with
their own money"* (`decision-engine.md` [2.4]).

### The two legs, precisely

- **Debit leg** — ACH pull from the user's checking into a platform funding account. Ordinary ACH via
  **Increase**. Plain ACH lands money in *our* account; it cannot route to an arbitrary card issuer.
- **Payoff leg** — land that money on the card at the issuer, via **Method** (a biller-payoff API
  reaching 15,000+ creditors). Method's payment *source* must be a platform funding account (an
  end-user's checking cannot be a source), which is why the money transits an account we hold.

### The custody consequence, taken on knowingly

Because the funds transit a platform account, this rung **commits to an FBO/custodial posture**
([5.2] in the origin, resolved). That turns on Reg E / GLBA / money-transmitter / reconciliation
obligations from the first *live* transfer — none of which block the *shadow* build, but all of which
are predecessors to turning `submit()` on. This is the single biggest reason `prd.md` [7.1] says the
distribution decision should settle first (an embedded partner may own the rail *and* the custody).
ADR-0006 records the commitment.

### What "done" means

No production money moves. Done is: the saga runs in shadow against real (fixture-attested) pilot
households and produces a tail-risk ledger; and U6's hard gate drives a **real** Increase sandbox
debit to settlement and a **real** Method sandbox payoff to `posted` and then a **forced `reversed`**,
asserting the ledger and `SWEEP_IN_FLIGHT` both unwind. A green *mocked* suite is explicitly not
evidence here — *"a mechanism built, tested, and never actually exercised"* is this repo's recurring
defect class (tickets `0021`, `0033`, `0038`).

---

## Key Technical Decisions

### KTD-1 — The `transfers` ledger is append-only by grant, like `plaid_transactions`

A state transition is a new row, never an `UPDATE`. Mirror `alembic/versions/0006_plaid_transactions.py`
(on `main`): grant `cfo_app` **SELECT + INSERT only** (privilege-layer append-only, not convention),
and put **no `UNIQUE`** on `provider_transfer_id` (a superseding transfer or a later status-transition
row shares references and a blanket unique would reject exactly the correction append-only exists to
allow). RLS is the standard two-statement `ENABLE` + `FORCE` + one `USING`/`WITH CHECK` policy keyed on
`current_setting('app.household_id', true)`.

### KTD-2 — The idempotency guard is `(household_id, decision_date)`, enforced by a slot lock — not a row UNIQUE

Corrected from `architecture.md` [5]'s stale `(user, date, decision_id)`: a same-day *re-decision*
mints a new `decision_id`, so a `decision_id`-keyed constraint would let a second debit slip the exact
double-debit guard it exists to be. A re-decision produces a **superseding** transfer against the same
`(household, decision_date)` slot; the invariant is **at most one transfer per slot reaches
`submitted`**. Implement as a `SELECT … FOR UPDATE` row lock on the slot (the pattern `backend/plaid/sync.py`
uses to serialize webhook-vs-poll races), **not** a UNIQUE constraint — proven under a real two-thread
test, not asserted single-threaded (learning from ticket `0037`). This `(household, decision_date)`
key is what maps to each vendor's `Idempotency-Key` header (KTD-4).

### KTD-3 — The port is real but asymmetric; `authorize` is client-side

`TransferProvider` = `authorize` · `submit` · `status` · `handle_return`, with a **debit** half
(Increase) and a **payoff** half (Method). Research refined each method:

- **`authorize`** is a **client-side** step for both vendors — the pre-flight balance re-check plus
  idempotency-key derivation. Neither vendor has a true two-phase authorize/submit: Increase's
  `pending_approval` is an internal review hold you don't call; Method's `dry_run` doesn't persist a
  resource. Do not model `authorize` as a vendor round-trip.
- **`submit`** is the vendor create call. **Increase:** `POST /ach_transfers` with a **negative signed
  `amount`** for a debit (there is no `direction` field — a real gotcha; the port takes an unsigned
  amount + direction enum and converts). **Method:** `POST /payments` (`source`→`destination`,
  `description` **≤10 chars**).
- **`status`** is asymmetric. **Method:** one field, `pending→processing→sent→posted`. **Increase:**
  has **no `settled` status** — settlement is derived from transfer `submitted` + the associated
  Transaction existing + no `returned` arriving within the return window (2 banking days typical, up
  to 60 for unauthorized-debit codes). So `status()` for Increase joins transfer-status + transaction
  + absence-of-return, and "settled" is a **timeout state our state machine imposes**, not one Increase
  hands us.
- **`handle_return`** differs in representation. **Method:** further transitions of the same Payment
  (`reversal_required` → `reversal_processing` → `reversed`). **Increase:** a separate return event +
  a new Transaction against the original transfer, with a NACHA `return_reason_code` (R01/R02/R03…).
  The internal ledger representation must not assume both are the same shape.

The engine boundary holds: nothing in `engine/` imports from `backend/transfer/`; the only coupling is
`Snapshot.sweeps_in_flight` (an input the backend populates) and the two reason codes.

### KTD-4 — Idempotency-key handling is per-vendor and derives from the saga run

Both use an `Idempotency-Key` header, but semantics differ: **Increase** returns `409
idempotency_key_already_used_error` on key-reuse-with-different-params; **Method** silently replays the
original response (including a replayed error). The activity wrapper needs per-vendor conflict handling.
The key is derived up front from the saga run identity (the `(household, decision_date)` slot + a
step suffix, e.g. `…-debit-submit`, `…-payoff-submit`), so a worker retry of the same step reuses the
same header value. (This is the discipline Temporal wouldn't give for free either — noted for the
later Temporal migration.)

### KTD-5 — Provider webhooks are a trust boundary, handled exactly like the Plaid doorbell

Both providers deliver settlement/returns by webhook. **Increase:** Standard Webhooks —
`webhook-id`/`webhook-timestamp`/`webhook-signature` (`v1,<base64 HMAC-SHA256>` over
`id.timestamp.body`), ~5-min freshness, up to 8 retries / 72h. **Method:** `method-webhook-signature`
(HMAC-SHA256 over `timestamp:body`) + `method-webhook-timestamp`, 5s/5-retry, and it **auto-disables
after 5 consecutive failures or >40% failure in 24h** — alert on the disable, not just per-delivery.
Follow `backend/plaid/webhook.py` (on `main`): verify signature + reject replays → persist the raw
payload **unscoped** under a dedup key with `UNIQUE … NULLS NOT DISTINCT` → enqueue via Cloud Tasks
only if newly inserted → **never work inline**. A reconciliation poll is the backstop (webhooks are
not sufficient alone — Increase exhausts retries after 72h; Method self-disables).

### KTD-6 — Provider-webhook tenancy: a `SECURITY DEFINER` lookup, per ADR-0005's rule

A provider webhook names a `provider_transfer_id`, not a `household_id`, and must resolve into the
FORCE'd, scoped `transfers` table before scope can be set. Reuse ADR-0005's shape (`backend/plaid/`'s
`plaid_household_for_item()`): a **narrowly-scoped `SECURITY DEFINER` function**
`transfer_household_for_provider_ref(provider, ref) → household_id` — never `BYPASSRLS` on the app role
(`assert_rls_binds()` refuses that). ADR-0005 [3] explicitly says any new definer function is a new
ADR, so this is recorded in **ADR-0006** alongside the custody commitment.

### KTD-7 — Two credential guards, not one

Reuse the `assert_plaid_tokens_safe_at_rest()` **env-flag guard shape** (`backend/db/session.py`) for
any per-item debit credential, but Method's **application-level API key** authenticates every call for
the whole tenant (not one item), so it goes to a **secrets manager from day one**, with sandbox and
production keys never interchangeable, and U6's sandbox work must not log it. The hard trigger for KMS
is **production `submit()` itself**, not merely a production env flag — the guard must refuse to move
real money against a plaintext credential, the way `assert_rls_binds()` refuses to start under a
bypass-capable role.

### KTD-8 — `target_card_id` naming

The engine dataclass field is `target_debt_id` but the persisted `decisions` column is `target_card_id`.
The `transfers` ledger references the card being paid; **store it as `target_card_id`** to match the
schema of record. The engine-vs-schema naming mismatch is logged as a flat-cost follow-up cleanup in
`docs/implementation-notes.md`, not resolved in this rung.

---

## High-Level Technical Design

*This illustrates the intended approach and is directional guidance for review, not implementation
specification. The implementing agent should treat it as context, not code to reproduce.*

### The state machine, mapped to both vendors

```
        our ledger state        Increase (debit leg)              Method (payoff leg)
        ----------------        --------------------              -------------------
        proposed                (client-side)                     (client-side)
        authorized              (client-side re-check + idem key)  (client-side re-check + idem key)
        submitted               POST /ach_transfers  → submitted   POST /payments → pending/processing
        pending                 submitted + txn, no return yet     processing → sent
        settled                 DERIVED: no return in window        posted
          ↘ returned/failed     return event + R-code / rejected    reversal_processing → reversed / failed
          ↘ cancelled           canceled (pre-submission)           canceled (only while pending)
```

### The saga, on Cloud Tasks (not Temporal)

```
decide() emits Decision(target_card_id, amount)
   │
   ▼
[authorize]  slot lock (household, decision_date) FOR UPDATE
             pre-flight re-check (fresh balance — see Deferred [5.3])
             derive idempotency keys; append ledger row `authorized`
   │
   ▼
[submit debit]  ShadowProvider: log no-op, append `submitted`   ← SHADOW STOPS HERE
                (live: Increase POST /ach_transfers, negative amount)
   │  ... webhook: verify → dedup → enqueue → worker advances ledger
   ▼
[debit settled] → [submit payoff] (Method) → [payoff posted]
   │
   ▼ on any return/reversal: append `returned`, run compensation, unwind SWEEP_IN_FLIGHT
reconciliation poll (Cloud Scheduler): walk non-terminal transfers, match provider truth
```

### Port interface sketch

```
TransferProvider (debit half = Increase, payoff half = Method):
    authorize(funding, dest, amount, direction, idem) -> Auth      # client-side; no vendor call
    submit(auth) -> ProviderRef                                    # vendor create
    status(ref) -> LedgerState                                     # Method: 1 field; Increase: join
    handle_return(ref) -> Return | None                            # representation differs per vendor

ShadowProvider implements the same interface: every method logs and advances the ledger; submit() calls no vendor.
```

---

## Prerequisites / Dependencies

- **~~Merge `main` into this branch.~~ DONE 2026-07-17.** `backend/plaid/`, tickets `0034`–`0038`, and
  ADR-0005 are now in the tree (merged; two `docs/` conflicts resolved). U1/U3 can build against the
  merged `plaid_items` schema and the transport patterns.
- **`0016` attestation.** No backend write-path exists. Shadow mode against an unattested population
  measures nothing (every unattested household is silently refused). This plan's default: **attest
  pilot households by fixture/seed** (U5/U6), and name the real attestation write-path as a follow-up.
- **Increase + Method sandbox credentials** for U6 (skip loudly via `pytest.mark.skipif` when absent,
  like `tests/test_plaid_sandbox.py`).
- **A `from-zero` migration check** — ticket `0033`'s known-open follow-up: no CI job migrates a fresh
  DB to head, so the `HOUSEHOLD_SCOPED`-import class of bug recurs silently. Adding a migration here is
  a good moment to add that CI job (flagged, not required).

---

## Implementation Units

### U1. The `transfers` ledger, RLS, and the migration

**Goal.** An append-only, RLS-scoped `transfers` table carrying the state machine and the idempotency
slot, added to `HOUSEHOLD_SCOPED` (8→9), with the IDOR fixture seeded so the leak test is meaningful.

**Requirements.** Origin U1; `architecture.md` [5]; KTD-1, KTD-2, KTD-8.

**Dependencies.** The `main` merge (Prerequisites). No plan-local predecessors.

**Files.**
- `alembic/versions/0008_transfers.py` (new — next revision after `0007`, confirmed post-merge)
- `backend/db/models.py` (add the `transfers` `Table`; add `"transfers"` to `HOUSEHOLD_SCOPED`)
- `backend/db/repository.py` (add `add_transfer(...)` and non-terminal-transfer queries, mirroring `add_plaid_transaction`)
- `tests/test_idor.py` (extend the `two_households` fixture to insert a real `transfers` row per household)
- `tests/test_schema.py` (grant/RLS assertions for the new table)
- `tests/test_transfers_ledger.py` (new)

**Approach.** Columns: `id`, `household_id`, `target_card_id` (KTD-8), `decision_id`, `decision_date`,
`leg` (`debit`/`payoff`), `state` (CHECK enumerating the KTD-3 states), `direction`, `amount`,
`provider` (`increase`/`method`), `provider_transfer_id` (nullable — set after `submit`),
`return_code` (nullable), `idempotency_key`, `created_at`. Append-only by grant (SELECT+INSERT only to
`cfo_app`); **no UNIQUE** on `provider_transfer_id`. The migration **hardcodes its own table name as a
literal** and never imports `HOUSEHOLD_SCOPED` (the `0033` trap, now proven three times). The slot
invariant (KTD-2) is enforced by the saga's `FOR UPDATE` lock, not a constraint here.

**Patterns to follow.** `alembic/versions/0006_plaid_transactions.py`, `0001_household_scoped_schema.py`
(RLS two-statement + policy), `backend/db/repository.py::add_plaid_transaction`.

**Test scenarios.**
- `cfo_app` can INSERT a transfer row and SELECT it back within its own household scope (happy path).
- `cfo_app` is refused UPDATE and DELETE on `transfers` at the privilege layer (append-only proven by the grant, not the app).
- A second household's connection reads zero `transfers` rows for the first household — the parametrized `HOUSEHOLD_SCOPED` leak test now covers `"transfers"` **with real rows seeded** (not a vacuous empty-set pass). *Covers the IDOR invariant.*
- A superseding transfer (same `(household, decision_date)`, new `decision_id`) inserts as a new row without a UNIQUE rejection.
- Inserting a row whose `state` is not in the CHECK set is rejected.
- `test_schema` confirms `transfers` has ENABLE + FORCE RLS and a policy keyed on `app.household_id`.

**Verification.** `test_idor.py` and `test_schema.py` pass with `transfers` in `HOUSEHOLD_SCOPED`; the fresh-DB migration reaches head; append-only is proven by a refused UPDATE as `cfo_app`.

---

### U2. The `TransferProvider` port + `ShadowProvider`

**Goal.** The two-leg port interface (KTD-3) and a `ShadowProvider` whose `submit` is a logged no-op
that advances the ledger — the first shipped state.

**Requirements.** Origin U2; KTD-3; `architecture.md` [6] (shadow first).

**Dependencies.** U1.

**Files.**
- `backend/transfer/__init__.py`, `backend/transfer/provider.py` (the port protocol + shared types), `backend/transfer/shadow.py` (`ShadowProvider`)
- `tests/test_transfer_shadow.py` (new)

**Approach.** Define the port as a `Protocol` with `authorize`/`submit`/`status`/`handle_return` and
the shared value types (`Auth`, `ProviderRef`, `LedgerState`, `Return`, a `direction` enum). Model the
asymmetry in the types, not by leaking vendor detail: `status` returns our `LedgerState`; the mapping
from each vendor's shape lives in that vendor's adapter (U3/U4), not the port. `ShadowProvider`
implements every method by logging and appending the next ledger row; `submit` calls no vendor and
returns a synthetic ref. Keep `backend/transfer/` free of any `engine/` import (the boundary).

**Patterns to follow.** The `Repository` seam in `backend/db/repository.py`; the dependency-free
discipline noted in `pyproject.toml` (engine/sim stay pure — the port lives in `backend/`).

**Test scenarios.**
- `ShadowProvider.submit()` advances the ledger to `submitted` and calls no network (assert no HTTP client is constructed / no outbound call).
- A full shadow run (`authorize → submit → status → settled`) appends the expected ledger rows in order, moving no money.
- `handle_return` on a shadow transfer appends a `returned` row.
- The port type-checks against both a shadow and a stub live adapter (structural typing holds).

**Verification.** A shadow sweep for a fixture household produces a complete ledger trail with zero outbound calls.

---

### U3. Debit-leg auth (Plaid Auth → Increase), Method Connect, the FBO funding account + credential guards

**Goal.** Establish the three things a real transfer needs: the debit-leg funding authorization (Plaid
Auth numbers → Increase), the payoff-leg destination (Method Connect discovering the card liability),
and the platform FBO funding account registered with both providers — behind the two credential guards
(KTD-7).

**Requirements.** Origin U3 (re-shaped per Decision 2); KTD-7; `architecture.md` [7.2].

**Dependencies.** The `main` merge (needs `plaid_items`); U1.

**Files.**
- `backend/transfer/increase.py` (debit adapter: Auth-numbers → `ach_transfers` counterparty / External Account)
- `backend/transfer/method.py` (payoff adapter: Connect destination-liability discovery)
- `backend/transfer/funding.py` (the platform FBO funding-account config/registration)
- `backend/db/session.py` (extend the startup guards: the processor/debit-credential env-flag guard; the Method-API-key secrets-manager requirement)
- `backend/plaid/` (reuse Plaid Auth retrieval; add `/auth/get` usage if not already present on `main`)
- `tests/test_transfer_credentials_guard.py`, `tests/test_transfer_funding.py` (new)

**Approach.** **Debit leg:** use Plaid **Auth** (`/auth/get`) on the already-linked item to get
account/routing numbers, pass them to Increase as the counterparty (or create an Increase External
Account) — **no processor token** (Increase isn't a Plaid processor partner). **Payoff leg:** Method
Connect discovers the destination card as a `liability` account. **FBO:** the platform funding account
is platform-owned (not household-scoped — it does **not** go in `HOUSEHOLD_SCOPED`); store its
identifiers in config/secrets. **Guards:** the debit credential reuses the
`assert_plaid_tokens_safe_at_rest()` env-flag shape; Method's tenant-wide API key must come from a
secrets manager with production `submit()` as the hard KMS trigger.

**Approach note.** *Funding the platform account itself (how user funds arrive to be paid out) is
unconfirmed in Method's docs (the "create a source account" guide 404'd during research) — verify with
Method before the live path. Shadow mode does not exercise it.* → Deferred implementation note.

**Patterns to follow.** `backend/plaid/link.py` / `deps.py` (token exchange, item access),
`assert_plaid_tokens_safe_at_rest()` in `backend/db/session.py`, ADR-0005's credential handling.

**Test scenarios.**
- The startup guard raises when a non-sandbox env is configured with a plaintext debit credential (mirror `assert_plaid_tokens_safe_at_rest`'s existing test).
- The startup guard raises when Method's API key is sourced from an env var / plaintext rather than the secrets manager in a production configuration.
- Plaid Auth numbers map into the Increase counterparty shape (unit test the adapter's translation, mocked HTTP).
- Method Connect returns a `liability`-typed destination for a known card; a non-liability account is rejected as a destination.
- The Method API key never appears in logs emitted by the funding/credential path (assert log capture is clean).

**Verification.** Guards fail-closed in a simulated production config; the adapters translate Auth
numbers and Connect liabilities correctly under mocked HTTP; no credential is logged.

**Execution note.** Start with a failing guard test for each credential before wiring the adapters — the guards are the security-load-bearing part.

---

### U4. The durable saga on Cloud Tasks: authorize → submit → poll/webhook → settle/return

**Goal.** The state machine as a durable, idempotent saga on the repo's Cloud Tasks + row-lock pattern:
pre-flight re-check, per-vendor idempotency, webhook verification + dedup + enqueue, the reconciliation
poll, and compensation on return. Owns ADR-0006.

**Requirements.** Origin U4; `architecture.md` [5]; KTD-2, KTD-3, KTD-4, KTD-5, KTD-6.

**Dependencies.** U1, U2, U3.

**Files.**
- `backend/transfer/saga.py` (the state machine: authorize → submit(debit) → settle → submit(payoff) → posted; compensation on return)
- `backend/transfer/webhook.py` (Increase + Method signature verification, replay rejection, raw unscoped store, dedup, enqueue)
- `backend/transfer/tasks.py` (Cloud Tasks worker that advances the ledger under the slot `FOR UPDATE` lock; OIDC-gated route)
- `backend/transfer/reconcile.py` (the poll: walk non-terminal transfers, match provider truth; Cloud Scheduler route, OIDC-gated)
- `alembic/versions/0009_transfer_webhooks_and_household_lookup.py` (unscoped raw `transfer_webhooks` store + the `transfer_household_for_provider_ref` `SECURITY DEFINER` function)
- `backend/main.py` (register the webhook / worker / reconcile routers)
- `docs/decisions/0006-*.md` (ADR: the FBO/custody commitment + the transfer-webhook tenancy exception)
- `tests/test_transfer_saga.py`, `tests/test_transfer_webhook.py`, `tests/test_transfer_reconcile.py` (new)

**Approach.** The saga is not a Temporal workflow (Decision 1); it's the Plaid-sync durability shape:
each webhook/poll event advances the ledger by appending a new row under a `SELECT … FOR UPDATE` lock
on the `(household, decision_date)` slot. Idempotency keys derive from the slot + step suffix (KTD-4),
with per-vendor 409-vs-replay handling. `authorize` runs the pre-flight re-check (Deferred [5.3] sizes
whether it re-reads balance live). Webhooks follow `backend/plaid/webhook.py` exactly (KTD-5); the raw
store dedups with `UNIQUE … NULLS NOT DISTINCT` (a status webhook before a `provider_transfer_id` is
assigned has a nullable key — `NULLS NOT DISTINCT` is required, learning from `0035`). Tenant
resolution uses the `SECURITY DEFINER` lookup (KTD-6). The reconciliation poll is the backstop for
Increase's 72h retry-exhaustion and Method's webhook auto-disable. Compensation on a return/reversal
appends `returned` and unwinds `SWEEP_IN_FLIGHT` (the socket U5 wires).

**Patterns to follow.** `backend/plaid/webhook.py` (verify → dedup → enqueue, never inline),
`backend/plaid/sync.py` (`SECURITY DEFINER` tenant resolver, `FOR UPDATE` serialization, separate
error-status transaction), `backend/plaid/oidc.py` (Cloud Tasks/Scheduler OIDC gating), ADR-0005 (the
unscoped-raw-store + definer-lookup precedent).

**Test scenarios.**
- **Idempotency (highest-stakes):** two saga runs on one `(household, decision_date)` slot produce **one** `submitted` debit — proven under two concurrent threads racing the slot lock, not single-threaded. *Covers KTD-2.*
- A same-day re-decision (new `decision_id`) produces a **superseding** transfer, and only one reaches `submitted`.
- Increase webhook: a valid signature within the freshness window is accepted; a tampered body, a stale timestamp, and a replayed `webhook-id` are each rejected before touching the saga.
- Method webhook: valid HMAC over `timestamp:body` accepted; forged/replayed rejected; a simulated webhook auto-disable condition raises an alert path.
- A status webhook whose dedup key has a NULL column dedups correctly (`NULLS NOT DISTINCT`) — a redelivery is a no-op.
- Webhook-vs-reconcile race on one transfer: the `FOR UPDATE` lock serializes them; the ledger gets one advancement, not two (two-thread test).
- `transfer_household_for_provider_ref` resolves a `provider_transfer_id` to the correct household and the worker binds that scope with `SET LOCAL` semantics (`household_scope`), never a raw `SET`.
- Return path: an Increase `returned` (with an R-code) and a Method `reversed` each append a `returned` ledger row and trigger compensation; the two vendors' differing return representations both land as our `returned` state.
- The reconciliation poll advances a transfer whose webhook never arrived (backstop proven).
- A non-retriable vendor code (Increase R02/R03/R04; Method 10003/10007/10008) is **not** blindly retried; a retriable one (R01/R09) is.

**Verification.** The full saga runs end-to-end against `ShadowProvider` (no money), and every webhook
trust-boundary and idempotency assertion passes; ADR-0006 is written and accepted.

**Execution note.** Write the concurrent double-run idempotency test first — it is the invariant the whole rung exists to protect.

---

### U5. The `SWEEP_IN_FLIGHT` engine feedback

**Goal.** Close the loop the engine already expects: an in-flight transfer makes the next
`assemble_snapshot()` carry `SWEEP_IN_FLIGHT`, and a returned/settled transfer unwinds it.

**Requirements.** Origin U5; `decision-engine.md` [2.4]/[9]; the origin's [4.3].

**Dependencies.** U4; the engine (`engine/models.py`, `engine/decide.py` — read-only).

**Files.**
- `backend/precompute.py` (`assemble_snapshot()` — replace the hardcoded `sweeps_in_flight=ZERO` with a real query over non-terminal `transfers`)
- `backend/readpath.py` and/or a new live-request assembly path (see Approach)
- `backend/db/repository.py` (a "sum of non-terminal transfer amounts for household" query)
- `tests/test_sweep_in_flight_feedback.py` (new); extend `tests/` around `precompute`/`decide` as needed

**Approach.** `assemble_snapshot()` in `backend/precompute.py` today hardcodes `sweeps_in_flight=ZERO`
and is the demo/walk-building path; `backend/readpath.py` serves *precomputed* snapshots and does not
assemble live ones. U5 must (a) make snapshot assembly read the real sum of amounts on transfers in
`submitted`/`pending` state for the household, and (b) determine whether a **live-request assembly
path** exists or must be built alongside — the engine socket is only meaningful on the path that feeds
a real production decision, not just the demo walk. A `returned` transfer removes its amount from the
in-flight sum (unwinds). Do **not** modify `engine/` — the engine already consumes
`Snapshot.sweeps_in_flight`; this is purely a backend input change.

**Patterns to follow.** `assemble_snapshot()` / `derive_portfolio()` in `backend/precompute.py`;
`engine/decide.py`'s existing `sweeps_in_flight > ZERO` refusal.

**Test scenarios.**
- A household with a transfer in `submitted` assembles a snapshot whose `sweeps_in_flight` equals that transfer's amount; `decide()` then refuses the next sweep with `SWEEP_IN_FLIGHT`. *Covers the origin's [4.3].*
- Two stacked in-flight transfers sum correctly into `sweeps_in_flight` (no second sweep stacks).
- A `returned`/`reversed` transfer unwinds — the next snapshot's `sweeps_in_flight` drops back, and a sweep is permitted again.
- A `settled`/terminal transfer no longer counts as in-flight.
- Cross-household: household A's in-flight transfer never appears in household B's snapshot (RLS scope holds through the assembly query).

**Verification.** `decide()` refuses a stacked sweep against a fixture household with a live in-flight
transfer; the refusal disappears once the transfer returns/settles.

---

### U6. The provider-sandbox hard gate (Increase + Method), IDOR + idempotency assertions

**Goal.** The unit that makes the other five real: a **real** Increase sandbox debit driven to
settlement and a **real** Method sandbox payoff driven to `posted` and then a **forced `reversed`**,
asserting the ledger and `SWEEP_IN_FLIGHT` both unwind — plus the IDOR and idempotency assertions, and
the shadow-decision replay through both sandboxes.

**Requirements.** Origin U6 + [6]; the repo's *"built, tested, never exercised"* discipline.

**Dependencies.** U1–U5.

**Files.**
- `tests/test_transfer_sandbox.py` (new — the hard gate; `skipif` without live sandbox creds, loudly)
- `tests/test_idor.py` (already extended in U1 — confirm the `transfers` leak test is non-vacuous)
- `backend/transfer/` (a small sandbox-simulation driver for Increase `/simulations/ach_transfers/{id}/{submit,settle,return}` and Method `/simulate/payments/{id}`)

**Approach.** Drive the **real** vendor sandboxes, not mocks. **Increase:**
`POST /simulations/ach_transfers/{id}/submit` → `/acknowledge` → `/settle`, then a separate run to
`/return` with a chosen `reason`. **Method:** `POST /simulate/payments/{id}` stepping
`pending→processing→sent→posted`, then a second run forcing `status=reversal_processing` with an
`error_code` → `reversed`. Assert the ledger advances through the KTD-3 states and that a forced
return/reversal appends `returned` and unwinds `SWEEP_IN_FLIGHT` (U5). Replay a sample of real
shadow-mode decisions through both sandboxes to retire integration risk (origin [0.3]). Document
provenance ("run green against real Increase + Method sandbox on <date>") and any reconciled surprises
— **budget for at least one vendor-model correction** (the Plaid harness `0038` found two; e.g.
Increase has no `settled` status, so the test must assert the *derived* settlement, not a webhook that
says "settled").

**Patterns to follow.** `tests/test_plaid_sandbox.py` (loud skip, real-vendor drive, provenance note,
reconciled-surprise documentation).

**Test scenarios.**
- Increase sandbox: authorize → submit → settle drives the ledger `submitted → (derived) settled`; assert settlement is inferred (no `settled` enum from Increase), per KTD-3.
- Increase sandbox: a forced `/return` with an R-code appends `returned` and unwinds `SWEEP_IN_FLIGHT`.
- Method sandbox: a payment driven to `posted`, then a forced `reversal_processing → reversed`, appends `returned` and unwinds `SWEEP_IN_FLIGHT`.
- The idempotency hard gate: two submits with the same derived key produce one vendor transfer (Increase replays with `Idempotent-Replayed: true`; Method replays the original) — one debit, one payoff.
- The `transfers` IDOR leak test is non-vacuous (real rows seeded in U1) and passes.
- Shadow-decision replay: a sample of real shadow ledger decisions runs through both sandboxes without error.
- The suite **skips loudly** (not silently) when sandbox credentials are absent.

**Verification.** The hard gate runs green against **both** real sandboxes including a forced return on
each leg; provenance and any vendor-reality corrections are recorded. A green mocked suite is explicitly
**not** accepted as evidence.

---

## Scope Boundaries

### In scope
The six units above: the `transfers` ledger, the two-leg port + shadow, the debit/payoff auth + FBO
funding account, the Cloud Tasks saga, the `SWEEP_IN_FLIGHT` feedback, and the real-sandbox hard gate —
all shadow-mode-first, moving no production money.

### Deferred to Follow-Up Work (plan-local sequencing)
- **The Temporal migration** — introduce Temporal (or another durable-execution substrate) at the same
  trigger that turns `submit()` on; the Cloud Tasks saga is the shadow-mode substrate (Decision 1).
- **The `0016` attestation write-path** — this rung fixture-attests pilot households; the real
  per-household attestation persistence + endpoint is its own ticket (owner: whichever of Link or this
  rung ships first, per origin [4.2]).
- **A from-zero migration CI job** — ticket `0033`'s open follow-up; good to add with this migration.
- **The `target_debt_id`/`target_card_id` naming reconciliation** (KTD-8) — flat-cost cleanup, logged.
- **Funding the platform FBO account** (how user funds arrive to be paid out) — unconfirmed in Method's
  docs; verify before the live path (U3 approach note).

### Deferred for later (event-triggered, per origin [3])
- **Turning `submit()` on in production** — trigger: a measured tail-risk number from shadow **and**
  the guarantee/reimbursement posture **and** the FBO/Reg E/MTL compliance build standing.
- **The full Reg E / GLBA / MTL compliance build** — trigger: the first production debit.
- **KMS for the debit credential and the Method API key** — trigger: production `submit()` (KTD-7).

### Outside this product's identity
- **Non-card destinations (reserve/T-bills)** — the engine won't sweep idle cash (`decision-engine.md`
  [4]) and emits no reserve decision; this rung pays debt only.

---

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A duplicate sweep = an overdraft (the highest-stakes failure) | Low if guarded | Severe | KTD-2 slot lock + KTD-4 per-vendor idempotency key; the concurrent double-run test is written first (U4) and is a hard gate (U6). |
| A forged/replayed provider webhook fakes a settlement or return | Medium | Severe | KTD-5 signature + replay verification before the callback touches the saga; reconciliation poll backstop. |
| RLS on the new `transfers` table is "built, tested, never exercised" | Medium (repo history) | High | U1 seeds real IDOR-fixture rows so the leak test is non-vacuous; U6 confirms. |
| Vendor reality diverges from the assumed model (no `settled` on Increase; funding-account mechanism unconfirmed) | High | Medium | U6 drives real sandboxes and budgets for ≥1 correction; the funding mechanism is flagged for direct verification. |
| Shadow run measures nothing (unattested population) | High if unguarded | High | Fixture-attest pilot households (Prerequisites); name the real write-path as a follow-up. |
| Building on a stale branch baseline (no `backend/plaid/`) | High if skipped | High | Merge `main` first (Prerequisites); mirrors the `0031` incident. |
| Cloud Tasks saga lacks Temporal's compensation ergonomics | Medium | Medium | Explicit compensation on return in U4; the Temporal migration is deferred to the money-on trigger, not skipped. |

---

## Deferred Implementation Notes (execution-time unknowns)

- **[5.3] Does the pre-flight re-check re-read balances live?** With real ACH + card-posting latency,
  "fresh" may require a synchronous `/accounts/balance/get` at authorize time rather than trusting the
  last sync. Size this in U4 against real sandbox latency; default to a live balance read at authorize.
- **Live-request snapshot assembly path** (U5) — confirm whether one exists or must be built; the demo
  `assemble_snapshot()` is not the production decision path.
- **Method funding-account mechanism** (U3) — verify directly with Method (docs guide 404'd).
- **Increase ⇄ Plaid** — confirm with Increase support that Auth-numbers (not a processor token) is the
  supported funding path; absence of a partner listing isn't proof.
- **ARM CI note** — if a Temporal migration later uses time-skipping tests, its test env is unsupported
  on ARM Linux; irrelevant to this rung (no Temporal) but noted for the follow-up.

---

## Review History

*Plan created 2026-07-17 from the re-based brainstorm. Research: `ce-repo-research-analyst` (repo
patterns), `ce-learnings-researcher` (institutional learnings — the `0033`/`0021`/`0037`/`0038`
precedents and the `main`-merge predecessor), and `ce-web-researcher` (Increase / Method / Temporal API
contracts, which drove the port refinements in KTD-3/KTD-4/KTD-5 and Decision 2). Two forks resolved by
the user: Cloud Tasks-now-Temporal-later, and Increase-via-Plaid-Auth-numbers.*
