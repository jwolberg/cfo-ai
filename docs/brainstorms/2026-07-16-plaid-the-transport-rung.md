# Plaid, and the plumbing that must exist before anyone links a bank

**Date:** 2026-07-16 · **Status:** scope, not yet planned · **Feeds:** a plan doc, then tickets
**Touches:** a new `backend/plaid/`, `backend/db/models.py`, a new migration, `backend/main.py`.
**`engine/`, `sim/`, and `mobile/` are not touched.**

**Fact-checked against the code on 2026-07-16**, and against Plaid's docs the same day — two of the
claims our own docs make about Plaid are now stale, and one of the three blockers is not in any of
them. Every structural claim below cites the line it came from.

---

## [0] What this actually is

The ask was *"scope out Plaid integrations so that we can have test customers add their bank info."*

**This document does not scope that.** It scopes the rung below it, deliberately, and the reason is
[0.3]: the thing standing between a linked bank account and a decision is not Plaid. Scoping the ask
turned up four findings, and they moved the recommendation rather than sizing it.

### [0.1] The ask hides a fork, and the two sides differ by months

"Test customers add their bank info" is two projects wearing one sentence:

- **Transport** — Link, token exchange, cursored sync, a webhook doorbell, rows in a table.
  Provable against Plaid's Sandbox with no real data, no PII, no identity, and no encryption.
- **Serving** — a linked household gets a forecast and a decision in the app. Needs everything in
  transport, *plus* the recurring-event detector ([0.3]), *plus* real identity, *plus* KMS token
  storage, *plus* a GLBA posture — and then [4.2] refuses roughly half of the customers anyway.

This document scopes **transport only**, against **Sandbox**, linked by **us**. That is the chosen
rung. What it costs in honesty is stated in [0.4] and [3]: **at the end of it, no test customer can
add their bank info, and nothing in the product changes.** What it buys is that the expensive rung
stops being a guess.

### [0.2] Plaid's gate is smaller than our docs claim — both of them are stale

`architecture.md` [7.4] calls Plaid's production-access review *"a real launch gate that asks for
exactly the above. Track it as a dependency, not a formality."* `prd.md` §6.3 says *"Plaid's own
production-access review is a real launch gate."*

As of **2026-04-15**, Plaid replaced Limited Production with a **Trial plan**: free, **real
production data**, **auto-approved** for most teams (a flagged application gets a Customer Oversight
follow-up in 2–3 business days), and its bundle includes **Transactions, Balance, and
Liabilities** — precisely the products `prd.md` §4.1 names — plus **Auth**, Identity, Assets,
Investments, and Statements. Trial teams also reach most OAuth institutions without the full
Production registration.

So for a pilot the size of this one, **there is no review to pass.** Both documents should be
corrected; the gate they describe is real only at full Production scale.

Two limits do bind, and they bind harder than the review did:

- **10 Production Items, and removing one does not free a slot.** An Item is one bank *login*. A
  household with checking + savings at one institution and a card at another is **2 Items**. Ten
  Items is realistically **four or five households**, permanently — not ten.
- **Applying for full Production is one-way.** You cannot revert to Trial afterward.

Neither limit touches this rung: Sandbox Items do not count against the Trial cap.

### [0.3] The blocker is ours, not Plaid's — and it is in none of our Plaid docs

`architecture.md` [7] lists the decisions to make before the first migration: authz, token storage,
GLBA, the production review. All four are real. **None of them is the thing that stops a linked
household from producing a decision.**

`derive_cash_events()` (`backend/precompute.py:316-419`) reads the **simulator's spec** —
`spec.payroll`, `spec.bills`, `spec.card.payment` — not transaction history. Its own docstring says
so: the events are *"read straight off the spec that generated the household… just handed it
earlier, and without the detector's errors."* The recurring-event detector does not exist.

This is the only derivation function with that property. `derive_card`, `classify_behavior`,
`observed_monthly_payment`, `observed_monthly_charges`, `infer_close_day`,
`detect_unmatched_payments`, `daily_discretionary_high`, `income_variation`, and `spend_30d_high`
are already pure functions of `History` (`backend/precompute.py:439-473`, `511-753`) and
would run on real rows tomorrow. `assemble_snapshot()` (`backend/precompute.py:817-881`) is
therefore the correct seam, exactly as `0022` and `architecture.md` [2] assume — with one hole in
it.

`Snapshot.events` (`engine/models.py:615-696`) is what `forecast.py` projects, and the forecast is
the product. **A real household linked today assembles a snapshot with no events, and therefore no
forecast.** The detector is the largest single unbuilt piece in the path from a bank login to a
sweep, it is not on `architecture.md` [7]'s list, and no ticket carries it.

It is also **the one piece Sandbox cannot help build** ([0.4]), which is why this rung stops short
of it rather than pretending to start it.

### [0.4] What Sandbox proves — and the limit that decides this rung's shape

Sandbox supports Transactions, Liabilities, and Balance; `/sandbox/public_token/create` mints an
Item for an arbitrary institution **without the Link UI at all**; `/sandbox/item/fire_webhook` fires
webhooks on demand; `/sandbox/item/reset_login` forces `ITEM_LOGIN_REQUIRED`. Credentials are
`user_good` / `pass_good`.

That last pair is worth more than it looks. `ConnectionState` (`engine/models.py:58-61`) is exactly
`HEALTHY` / `LOGIN_REQUIRED` / `DISCONNECTED`, and `architecture.md` [3.1] says every item's
`last_successful_sync_at` *becomes* `Account.balance_age_days` — *"the freshness gate is the thing
standing between a dropped webhook and an overdraft."* **`/sandbox/item/reset_login` exercises that
gate for the first time.** It is currently a field the seeder sets to a constant.

And the limit, from Plaid's own documentation: **Sandbox data is synthetic, and it is not internally
consistent.** *"Sandbox data is not always based on a consistent data source across different API
calls"* — transaction history may contradict the account balance, and it *"does not reflect the full
scope and complexity of data that can exist in Production."*

That is not a footnote; it is the boundary of this rung. Our derivation **walks history to compute a
balance**: `derive_card` reconstructs `unbilled_balance` from `card_charged_between()` over the
history as of today, then `statement_balance` from the ledger balance minus it
(`backend/precompute.py:683-684`). Against a data source whose
transactions disagree with its balances, that arithmetic produces garbage, and **you cannot tell
your bug from Plaid's inconsistency.**

So the boundary is sharp and it is not a matter of taste:

| Sandbox **can** prove | Sandbox **cannot** prove |
|---|---|
| Link, token exchange, item storage | Normalization — pending→posted needs real institution behavior |
| `/transactions/sync` cursor mechanics, resumability, redelivery as a no-op | Derivation correctness — the data does not reconcile |
| The webhook doorbell: dedup, enqueue, never work inline | The recurring-event detector ([0.3]) |
| Item error states → `ConnectionState` → `balance_age_days` | **Anything about the world** |

The last cell is the one to keep. This repo's central limitation — *"the population is synthetic,
and its spending was generated by the same assumptions the engine forecasts with, so it can measure
the code and cannot yet measure the world"* (`prd.md` §8.1) — **is not repaired by Plaid Sandbox.**
Sandbox swaps our synthetic data for Plaid's. This rung buys engineering de-risking. It buys **zero**
measurement, and any claim otherwise should be challenged in review.

---

## [1] Scope

**In.** A `plaid_items` table with RLS. A raw webhook store keyed for dedup. An append-only
`plaid_transactions` table. A cursored `/transactions/sync` loop that is idempotent and resumable. A
Sandbox harness that exercises all of it end to end.

**Out.** The detector ([0.3]). Identity/Clerk. KMS. Normalization. The Link UI. Any wiring into
`assemble_snapshot()`, `accounts`, `cards`, or the served surface. See [3] for the trigger on each.

**The rung ends at rows in a table.** The seam to `assemble_snapshot()` is named and not crossed.

---

## [2] The units

| # | Unit | Depends on |
|---|---|---|
| U1 | `plaid_items` + RLS + the migration | — |
| U2 | The webhook doorbell and the raw store | U1 |
| U3 | `plaid_transactions`, append-only | U1 |
| U4 | The cursored sync loop | U1, U3 |
| U5 | The Sandbox harness — the unit that makes the other four real | U1–U4 |

### U1 — `plaid_items`, and the migration trap that is already documented

Columns: `id`, `household_id`, `plaid_item_id`, `institution_id`, `access_token`, `cursor`,
`status`, `last_successful_sync_at`, `error_code`, `created_at`. RLS follows `0001`/`0004` exactly —
`ENABLE` **and** `FORCE ROW LEVEL SECURITY`, a `USING`/`WITH CHECK` policy on
`current_setting('app.household_id', true)`, a grant to `cfo_app`, and the table added to
`HOUSEHOLD_SCOPED` (`backend/db/models.py:251-258`), taking the scoped count from **six to seven**.

**Read `0033` before writing the migration.** Adding a table to `HOUSEHOLD_SCOPED` is precisely the
operation that broke last time: a migration that imported the live constant and iterated it was
invisible *"for exactly as long as the constant stood still — which was every day until `0031` added
a table to it."* This unit adds a table to it. The migration must hardcode its own list.

**And `_assert_migrated()` will refuse to start** against an unmigrated Neon, correctly — the same
trap that left the deploy stale for eleven tickets. Neon must be migrated before this deploys.
`docs/runbooks/deploy.md` [2] is the step, and per memory, **measure the runbook's claims rather
than following them.**

### U2 — The doorbell, and a chicken-and-egg RLS problem worth deciding on purpose

`architecture.md` [3.1] is unambiguous about the shape: persist the raw payload under a natural dedup
key (`item_id` + `webhook_code` + cursor), enqueue, and **never work inline** — *"a slow handler
causes retries, which cause the duplicates you are trying to avoid."*

**But the raw store cannot be household-scoped at write time.** Plaid's webhook carries
`item_id`, not `household_id`. Resolving one to the other requires reading `plaid_items` — which is
RLS-scoped and needs `app.household_id` *already set*. The handler does not know it yet. RLS fails
closed (`current_setting(..., true)` returns NULL, NULL never equals a household_id), so the insert
is refused and so is the lookup.

Three ways out, and this is a real decision, not a detail:

1. The raw webhook table is **unscoped**, like `households` — the enqueued worker resolves the
   household and everything downstream is scoped. Narrow, deliberate, mirrors the one exception that
   already exists (`GET /households`, `backend/main.py:325-333`).
2. A **narrow unscoped lookup** of `plaid_item_id → household_id`, then scope immediately.
3. `plaid_item_id` is globally unique, so the doorbell writes it and resolution is a worker's job.

(1) is the smallest and it keeps every *financial* row scoped. It is also the first hole in "every
table is scoped by `household_id`" (`architecture.md` [4]), so it belongs in an ADR, not a commit.

### U3 — `plaid_transactions`, and the table that dies for it

Append-only; corrections are new rows (`architecture.md` [4]). Sync returns added/modified/removed —
**all three are inserts**, never an `UPDATE` or a `DELETE`.

`pending_transaction_id` gets a **column** here and no logic — the reconciliation that consumes it is
normalization, which is [3]'s, not this rung's.

**This unit is what eventually deletes `spend_projections`.** `backend/db/models.py:215-232` says so
in its first line: *"The one table here that ingest deletes. Ticket 0031."* It does not die in this
rung — the seam is not crossed — but this is the table it was waiting for, and the plan doc should
say when it dies rather than leaving a table annotated for a deletion nobody scheduled.

### U4 — The cursored sync loop

`/transactions/sync` per item, cursor persisted, redelivery a no-op by construction. A **nightly
reconciliation poll runs for every item regardless of webhooks** (`architecture.md` [3.1]) — because
deliveries will be missed, *"and we would rather find out in hours than when someone's rent
bounces."* On success, stamp `last_successful_sync_at`; on `ITEM_LOGIN_REQUIRED`, set `status` and
stop.

That field is not bookkeeping. It becomes `Account.balance_age_days`, which is the freshness gate.

### U5 — The Sandbox harness, and why it is the point

`/sandbox/public_token/create` → exchange → sync → `/sandbox/item/fire_webhook` → sync again → assert
the redelivery changed nothing → `/sandbox/item/reset_login` → assert `status` becomes
`LOGIN_REQUIRED` and the sync loop stops.

**This unit is the reason to do the other four.** The tickets README names the pattern every defect
in this repo has shared: *"a mechanism that was built, tested, and never actually exercised… None of
them had a symptom. All of them had a green test."* An ingest path with unit tests and no Sandbox
run would be the next entry on that list, and it would be the most expensive one, because the thing
it would be wrong about is someone's bank.

Because `/sandbox/public_token/create` bypasses Link entirely, **U5 needs no UI** — which is what
lets the Link flow ([3]) leave this rung without leaving it unproven.

---

## [3] What this rung does not build, and the trigger for each

| Not built | Trigger |
|---|---|
| **The recurring-event detector** ([0.3]) | Serving a real household. It is the gating piece, it needs real data to build against, and it deserves its own brainstorm. |
| **Link UI + the first write path** ([4.1]) | A person other than us linking an account. |
| **Clerk / identity** | The first **real** token. Internal-only is what licenses the deferral — and only while the data is Sandbox. |
| **KMS envelope encryption** | The first real token. `households.dek_id` (`backend/db/models.py:67-74`) is a column with no key behind it, and its own comment says the wiring *"lands with Plaid."* See [5.1] — this is the one deferral I am least sure of. |
| **Normalization** (pending→posted, internal transfers) | Real data. Sandbox cannot validate it ([0.4]), and getting it wrong is one-directional: double-counting under-sweeps, dropping overdrafts. |
| **GLBA posture** | The first real third party's data. Internal-only defers the *legal* artifact, not the engineering. |

Every trigger is an **event, not a date** — `architecture.md` [4.1]'s rule, applied.

---

## [4] Findings that were not in the ask

### [4.1] Plaid Link would be this backend's first write path — and so is the attestation nobody built

`POST /assistant/message` (`backend/main.py:522`) is the only non-GET route in the service, and it
writes nothing. **This backend has no write path at all.**

That is exactly why `0016` never built the attestation action: *"attesting is a **write**, and this
backend has no database by design (ADR `0002`)"* — leaving `CARD_COVERAGE_INCOMPLETE` as *"the one
refusal in this feature nobody has seen end-to-end in the product."* ADR `0002` has since been
superseded by `0004`, so the reason is gone but the gap is not.

**These are the same missing capability.** `CoverageState.UNATTESTED` is the default
(`engine/models.py:510`) and blocks every sweep (`engine/decide.py:405-406`) — *"Attestation is
therefore a real onboarding gate, not a checkbox"* (`decide.py:219`). So a linked household is
refused for being unattested until a write path exists, and Link **is** a write path. Whoever builds
Link should expect to close `0016` on the way through, and the plan doc should say so rather than
letting two tickets discover the same endpoint independently.

### [4.2] `0029` decides whether the next rung is worth climbing, and it has no safe fix

Real households are not all paid biweekly. `INCOME_BUCKET_DAYS = 28` divides evenly into a biweekly
calendar **and no other**. Measured, on archetypes whose income by construction never varied
(`payroll.variation = 0.02`):

| archetype | true variation | measured | days over the 0.25 gate |
|---|---|---|---|
| B semimonthly | 0.02 | up to **0.326** | **33/90** |
| C monthly | 0.02 | up to **0.707** | **19/90** |

So roughly half of any realistic set of test customers would be refused by a **calendar artifact**.

**And it cannot simply be fixed first.** `0029` is explicit that fixing the gate is a *loosening*
that hands those days back to a forecast which breaches **19.7%** of them — *"the broken gate is
currently the thing standing between a semimonthly household and a forecast that breaches 19.7%
of the time."* It is sequenced behind the forecast, not ahead of it.

This does not touch the transport rung. It should be settled before anyone promises a **serving**
one, because the honest version of that promise today is *"link your bank and be refused about half
the time, for a reason that is our bug."*

---

## [5] Open questions

### [5.1] Does `access_token` get encrypted in this rung, or at the first real token?

A Sandbox token protects nothing, so the trigger-based answer ([3]) says defer. **The argument
against deferring is that the trigger fires quietly.** The day someone swaps `PLAID_ENV` to
`production` and links their own checking account, a plaintext column becomes a real credential with
no migration, no review, and no test failing. `architecture.md` [7.2] wants KMS envelope encryption
plus a rotation and revocation runbook tied to item-error webhooks — that is not a thing anyone
retrofits in the same afternoon they flip an env var.

Cheapest honest option: build the column, defer KMS, and make the **env flag the guard** — the
service refuses to start with `PLAID_ENV != sandbox` while `dek_id IS NULL`. Same shape as
`assert_rls_binds()` (`backend/db/session.py:34-68`), which already refuses to start against a role
that could bypass RLS. That pattern is in this repo and it works.

### [5.2] The queue — and a dependency `architecture.md` [1.2] deliberately cut

The doorbell must enqueue rather than work inline, and this stack has no queue. [1.2] cut Temporal
(*"adopt it when money moves"*) and Redis (*"no cache-shaped access pattern exists"*) with reasons
that both still hold and neither of which is about this. Cloud Tasks is the GCP-local answer and is a
new dependency.

**Or the doorbell is deferred with the Link UI**, and this rung is the nightly poll only (U4), which
U5 can exercise just as well. That is smaller, keeps the dependency question shut, and loses the one
piece of transport that is genuinely hard to get right later. Worth deciding explicitly rather than
defaulting.

### [5.3] Which household does a Sandbox item attach to?

There is no owner column and no `users` table — `households` is `id`, `archetype`, `dek_id`,
`created_at`, `deleted_at` (`backend/db/models.py:59-79`). For internal Sandbox linking, attaching an
item to a hand-picked `household_id` is fine and should be stated as a **fixture**, not a design.

The question it defers: a linked household is not an `archetype`, and `households.archetype`
(nullable) is the only thing distinguishing seeded rows today. A real household is the first row
where it is NULL and means it.

### [5.4] Do Plaid `account_id`s map onto `accounts`/`cards`, or replace them?

Today's `accounts.id` / `cards.id` are seeder-invented strings. This rung does not touch either table
— transactions land in `plaid_transactions` and stop. But the mapping is the first question the
serving rung asks, and `accounts` already has a `connection` column
(`ConnectionState`) that is *not* a Plaid reference and looks exactly like one.

---

## [6] How this gets verified

U5 **is** the verification, and it is a hard gate: a Sandbox run that creates an item, syncs,
redelivers, and breaks the login — not a mocked one. Plus the `0021` IDOR suite extended to
`plaid_items` and `plaid_transactions`, since RLS on a new scoped table is precisely the mechanism
that has been *"built, tested, and never actually exercised"* — the tickets README catalogs **six**
such defects, two of them RLS itself.

**A green test suite is not evidence here.** Every one of those six had one.
