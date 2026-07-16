# Architecture

> Evergreen system overview — **edit in place** (unlike append-only ADRs). Headings carry
> `[N]` / `[N.M]` anchors so any part is greppable (`grep -n "\[2\]" docs/architecture.md`)
> and referenceable as `ARCH#2`.

anchor: ARCH

**Version:** v1 (2026-07-12). Supersedes the v0 advice-only design (see git history —
it planned Temporal, an event-driven pipeline, an append-only ledger, and a
provider-abstraction layer for a product that showed the user a number and moved no
money).

**Status:** partly built, and the line moved on 2026-07-16. [`engine/`](../engine),
[`sim/`](../sim), [`backend/`](../backend), and [`mobile/`](../mobile) exist and are described as
built — [3.4], [3.6], [3.7]. **Ingest, normalization, money movement, and auth do not** — [3.1],
[3.2], [5], and Clerk in [2] are still intent, and should be read as *what we would build and in
what order*, not as description.

**The decision log ([3.3]) is the hard case, and it is the one to read first.** Its schema is
built — tables, forced RLS, partitions, a scoping repository, an IDOR suite. Nothing calls it.
The service serves a **committed JSON artifact** and holds no write path, so [4]'s "system of
record" is intent while `backend/data/decisions.json` is fact. [3.6] says so plainly rather than
letting this section carry it alone.

---

## [1] Overview

The system moves a household's surplus cash onto their most expensive debt, without asking — and
refuses to move anything on days it cannot be sure. See [`prd.md`](./prd.md).

**It watches daily and moves weekly**, and those are two different things ([`prd.md`](./prd.md)
§2.4). The forecast runs every day and a held day is still graded; the cadence limits what we *do*,
never what we *know*. This section read "daily" until 2026-07-16, which was the same fossil §2.4
was written to kill — inherited from the v0 advice product, whose output was a notification.

### [1.1] Principles

1. **The decision is deterministic.** No LLM, no clock, no randomness anywhere near a
   dollar amount. The LLM narrates the decision's reasons; it never produces them.
2. **Refusal is the default.** Every gate fails closed. A day with no sweep is the system
   working.
3. **Inputs are snapshotted, not referenced.** Plaid rewrites history beneath us; a
   decision that cannot be replayed cannot be explained or audited.
4. **Money arrives late and small; it leaves early and large.** Every uncertainty resolves
   against the user's interest.
5. **Build the smallest thing that can be wrong in public.** Shadow mode before sweeps,
   small caps before large ones.

### [1.2] What we are deliberately not building yet

Cut from the v0 design, and why:

| Cut | Why |
| --- | --- |
| **Temporal** | It earns its keep on durable multi-step sagas with compensation — i.e. the ACH payment flow. Adopt it *when money moves* ([5]), not for a daily scheduled job. |
| **Redis** | No cache-shaped access pattern exists. One decision per user per day. |
| **Cloud Storage** | Nothing stores blobs. |
| **`FinancialProvider` abstraction** | MX and Finicity are hypothetical. You cannot design the seam from n=1 — you will guess wrong and reshape it anyway. Call Plaid directly; extract the interface when a second provider is actually underway. |
| **Notification service as a component** | Not a feature in the PRD. A sweep confirmation is an email/push call, not a subsystem. |
| **"Financial ledger"** framing | Accounting-system vocabulary for a system that (today) moves no money. What we need is narrower and stronger: an append-only **decision log** ([3.3]). |

---

## [2] Shape

```
Plaid ──webhook──> ingest ──> sync (cursored pull) ──> normalize ──> Postgres
                                                                        │
                              Cloud Scheduler ──daily──> build Snapshot ┘
                                                              │
                                                          engine.decide()   ← deterministic
                                                              │
                                              ┌───────────────┴───────────────┐
                                          REFUSE                           SWEEP
                                              │                               │
                                       log + explain              payment saga ([5])
                                                                              │
                                                                    log + explain + notify
```

That diagram is the **intended** system. This is the one that runs today:

```
sim.generate() ──> History ──> precompute.walk() ──> engine.decide()
                                                          │
                                                   decisions.json          ← committed to the repo
                                                          │
                                          FastAPI on Cloud Run (read-only, in memory)
                                                          │
                                     Expo web export on Firebase Hosting (cfo-ai-1.web.app)
```

Postgres sits **beside** that pipeline, not inside it — [3.6]. Nothing to the left of
`decisions.json` runs in production at all: the artifact is generated on a developer's machine and
committed, and the service reads it at startup.

Thinnest stack that ships this: **Expo / React Native** with `react-native-web`, exported to
static web (built) · **FastAPI on Cloud Run** (built; API only — there are no jobs yet) ·
**Postgres** (built, unwired — [3.6]) · **Cloud Scheduler** (the daily run — **not built**;
nothing is scheduled, because nothing ingests) · **Clerk** (auth — **not built**; the API takes a
shared key) · **Secret Manager** (the deploy passes the API keys as `--set-secrets` references;
Neon's connection string is not yet among them — ticket `0026`) · **Sentry** (**not built**;
it appears nowhere in the tree). Add anything else only when a measured problem demands it.

**The web frontend is an Expo web export, not Next.js.** This line said Next.js from v1 until
2026-07-16 and the frontend was never built that way — [3.7]. It is corrected here rather than
footnoted, because a stack list that names a framework nobody chose is how a reader ends up
arguing from the wrong constraints.

Where this says **Postgres**, read [4.1] — the store is Neon through the demo and the move to
Cloud SQL has a trigger but not yet an argument.

---

## [3] Components

### [3.1] Ingest & sync (not yet built)

The webhook is a **doorbell, not a delivery**. Plaid webhooks are at-least-once and
unordered, so the handler does exactly two things: persist the raw payload under a natural
dedup key (`item_id` + `webhook_code` + cursor), and enqueue. It never does work inline —
a slow handler causes retries, which cause the duplicates you are trying to avoid.

All state comes from a **cursored pull** (`/transactions/sync`), which makes redelivery a
no-op. A **nightly reconciliation poll** runs for every item regardless of webhooks,
because deliveries *will* be missed and we would rather find out in hours than when
someone's rent bounces.

Every item carries `last_successful_sync_at`. That value becomes `Account.balance_age_days`
in the engine — the freshness gate is the thing standing between a dropped webhook and an
overdraft.

### [3.2] Normalization (not yet built)

The two hard problems, each of which corrupts the forecast *silently* if wrong:

- **Pending → posted reconciliation.** `pending_transaction_id` when the institution
  provides it; otherwise fuzzy-match on account, sign, amount (tolerance asymmetric —
  posted ≥ pending is normal), date window, and normalized merchant. **An ambiguous match
  is not a match**: keep the pending charge alive. Double-counting makes us under-sweep;
  dropping makes us overdraft. The error is one-directional and the design follows it.
- **Internal transfer detection.** Pair-match opposite signs across the user's own
  accounts. Critically, **our own sweeps must reconcile against our own payment records
  before they come back through Plaid** — otherwise they score as discretionary spending,
  the forecast concludes the user is burning cash, and the product slowly strangles itself.

Feeds the **recurring-event engine**, which is where `CashEvent.confidence` comes from.
It needs ~2 observed cycles of an obligation to know anything, hence the engine's 60-day
cold-start gate.

### [3.3] The decision log — append-only, and the auditability is real (schema built, unwired)

Each run persists the **entire frozen `Snapshot`**, the `Decision`, and the engine version.

**Today no run persists anything** — the tables exist and the walk does not use them ([3.6]).
Read this section as the argument for the schema, which is built, rather than as a description of
a running write path, which does not exist.

This is the load-bearing design choice in the system, and it buys three things at once:

1. **Explanation that stays true.** "Why did you move $220 that Tuesday" is answerable
   forever, even after Plaid has replaced the underlying transactions.
2. **Audit.** GLBA/UDAAP: show a regulator exactly why this consumer was debited.
3. **Backtest.** Replay any new engine version across every historical snapshot and ask:
   *would this have overdrafted anyone?* That is shadow mode ([6]), and it exists only
   because of this choice.

Store the inputs, not references to the inputs. The difference between an audit trail and
a story.

### [3.4] The engine

[`engine/`](../engine) — built. See [`decision-engine.md`](./decision-engine.md).

Pure function: `Snapshot → Decision`. No I/O.

### [3.5] Explanation

The LLM renders `Decision.reasons` into prose. It receives the reasons, not the raw
financial data, and it is never in the decision path.

**Transaction descriptions are attacker-influenced text** (a merchant controls the string
that appears on a statement). Treat all of it as untrusted: constrain LLM output to a
fixed schema, and never let LLM output touch control flow.

### [3.6] The backend — built

[`backend/`](../backend) is a FastAPI service that serves **one demo household from a committed
artifact**. Python ≥3.10 and the `anthropic` SDK. SQLAlchemy **Core** (never the ORM), Alembic, and
`psycopg` are an optional `[api]` extra rather than a base dependency — which is not a packaging
detail, see below.

| Module | What it does |
| --- | --- |
| [`precompute.py`](../backend/precompute.py) | **The walk.** Steps a household day by day, builds each `Snapshot`, calls `decide()`. One `walk()`, three consumers: the artifact builder, the replay driver, and (next) the seeder. There were 2.5 copies and they had drifted — ticket `0019`. |
| [`replay.py`](../backend/replay.py) | Drives the walk and grades every day it can honestly grade, against realized history rather than the engine's own sweep-adjusted path. A blocking refusal never ran a forecast, so it is **not** graded — scoring it zero would look like a perfect forecast. |
| [`calibrate.py`](../backend/calibrate.py) | Grades a **population** — 60 synthetic households, 3 shapes × 20 seeds — at every setting of the spend dial. [`prd.md`](./prd.md) §5.2: a guardrail measured on one household is not measured. |
| [`codec.py`](../backend/codec.py) | Tagged-scalar JSON. JSON has no decimal type, and a cent through a float is not the cent the engine decided on. Type-driven, so it **cannot drift from the dataclass**. |
| [`artifact.py`](../backend/artifact.py) | The committed decision history, and a `validate()` that runs on every decode. |
| [`assistant.py`](../backend/assistant.py) | The only path that costs money ([3.5]). |
| [`auth.py`](../backend/auth.py) | The API-key gate. Guards every route but `/health`. |
| [`db/`](../backend/db) | Postgres — schema, repository, `SnapshotStore`. **Not in the serving path.** |

**The API.** Five routes, all in `main.py`; everything but `/health` requires `X-API-Key`. All
money is serialized as **strings**, never JSON numbers — the same reason `codec.py` exists.

| | |
| --- | --- |
| `GET /health` | liveness |
| `GET /decisions` | the feed: window, summary, decisions newest-first |
| `GET /spend` | this cycle, last cycle, and what normal looks like |
| `GET /decisions/{day}/explain` | one day, plus narration from `engine/explain.py`. An unknown day is `404 no_record` |
| `POST /assistant/message` | the LLM turn. The only endpoint that costs money |

**What serves a request today is a JSON file.** `main.py` loads
[`data/decisions.json`](../backend/data) once at startup, validates it, and serves it from memory
for the process lifetime. There is **no write path**, and the artifact is not generated in
production — it is built on a developer's machine by `python -m backend.precompute` and committed
to the repo, because the buildpack deploy packages whatever is in the source tree.

**And `db/` is wired to nothing.** `backend.db` is imported by exactly three kinds of caller:
Alembic, itself, and its own tests. No route, and no walk, has ever opened a connection to it.
[`backend/requirements.txt`](../backend/requirements.txt) — what Cloud Run actually installs — is
`fastapi`, `uvicorn`, `anthropic`; **the deployed image contains no database driver at all.**

**That is deliberate, and it is worth being precise about why**, because it looks exactly like the
failure this repo keeps finding in itself and is not one.
[ADR-0004](./decisions/0004-postgres-scoped-by-household.md) decides it explicitly — *"decide now,
wire when something real needs it."* Tenancy and provenance are the expensive halves to retrofit:
a column added later is a migration, but a scoping key added later is every query in the system.
So the schema is built ahead of its caller **on purpose**, and ticket `0024` is the caller.

The distinction that matters: `0019`–`0022`'s defects were mechanisms *believed* to be working.
This one is **known** to be unwired, and that is the whole of why it is safe. The residual risk is
still real, because RLS is precisely the kind of mechanism that reports green while doing nothing —
which is not a hypothetical here. It already happened twice on this schema: the IDOR suite ran as
the superuser that owns the tables, and Neon's default role bypasses RLS outright.

**The guard that came out of it.** [`db/session.py`](../backend/db/session.py)'s `assert_rls_binds()`
refuses to start under a role that is `rolsuper` or `rolbypassrls`. Neon's default `neondb_owner`
has the latter: connected as it and scoped to one household, a `SELECT` returned **both**. Pasting
the connection string Neon hands you into `DATABASE_URL` would ship [4]'s two authz layers as one,
with the IDOR suite green throughout. The intended runtime role is `cfo_runtime`, holding
`cfo_app`. Scoping is bound transaction-locally via `set_config(..., is_local => true)`, never on
the connection — Neon pools connections, and a connection-scoped variable leaks across requests.

**Deployment.** [`Procfile`](../Procfile) → uvicorn on Cloud Run, `gcloud run deploy --source .`,
no Dockerfile. **`--workers 1` is a spend control, not a throughput compromise:** the rate cap on
`/assistant/message` lives in process memory, and it is the only thing bounding Anthropic spend if
the public API key leaks. A second worker is a second counter and a doubled ceiling.

### [3.7] The frontend — built

[`mobile/`](../mobile) — Expo (SDK 57) / React Native in TypeScript under `strict`, with
`react-native-web` so one source runs on a phone and in a browser. Two screens and a modal:
[`Dashboard.tsx`](../mobile/src/screens/Dashboard.tsx) (the decision feed),
[`Spending.tsx`](../mobile/src/screens/Spending.tsx), and
[`ExplainModal.tsx`](../mobile/src/screens/ExplainModal.tsx) (the narration and the assistant).

**It carries no navigation, state-management, or data-fetching library.** The tabs are a `useState`
over two values; both screens stay mounted and are toggled with `display: none`, so switching does
not refetch or lose scroll. `App.tsx` argues the case: two screens, no nesting, no deep links — when
a third screen or a shareable link earns the router, the router earns its dependency.

**Every request goes through one function.** `api/client.ts`'s `request<T>()` is the only `fetch`
in the app. It carries a closed error taxonomy — `timeout · network · unauthorized · no_record ·
server · malformed` — and each screen renders a discriminated union of loading / ready / failed
rather than a boolean. **A 404 maps to `no_record`, an answer rather than a failure**, which is the
client-side shape of [1.1]'s "refusal is the default." Timeouts are 8s, and 60s for the assistant.

**The client cannot invent a financial claim, by construction.** `reason_codes` and `reasons[].text`
are opaque strings to it; it renders what `engine/explain.py` produced and inspects exactly one code
by name (`no_debt`, to say "paid off"). Money is formatted by string surgery and never parsed into a
float — the same rule as `codec.py`, enforced at the other end of the wire.

**Refusals are not errors, and the palette says so.** `theme.ts` contains no red. A refusal is deep
green and a sweep is blue. Days with no sweep are the product working ([1.1]); a UI that painted
them as failures would be arguing against the product on the one surface the user actually reads.

**Auth is a shared API key inlined into the web bundle** at build time (`EXPO_PUBLIC_API_KEY`) — a
documented trade-off, not an oversight. Synthetic households have no owner to authenticate as, and
Clerk arrives with Plaid.

It is also the reason [7]'s authz pattern is only **half**-real, and that half is the uncomfortable
one: the IDOR suite is genuine, but the identity feeding it is a key any reader of the bundle can
extract. **An IDOR suite is reassuring in a way a shared key does not earn.** Ticket `0021` asked
for that to be stated in [`USERS.md`](../USERS.md) rather than only in a code comment; it is not
there yet, so it is stated here.

**Not built: the attestation action.** `UNATTESTED` / `CARD_COVERAGE_INCOMPLETE` appear nowhere in
`mobile/` — attesting is a *write*, and this frontend talks to a service with no write path. The
refusal reaches the feed as ordinary text, and no one has ever cleared it end to end.

**Deployed by hand.** `npm run deploy:web` → `expo export --platform web` → Firebase Hosting
(`cfo-ai-1.web.app`). CI typechecks and tests `mobile/` and does **not** deploy it.

---

## [4] Data model (sketch)

`users` · `items` (a Plaid connection, with `last_successful_sync_at` and connection
state) · `accounts` · `transactions` (with `pending_transaction_id`, a `reconciled_with`
link, and an `internal_transfer_pair` link — **append-only, corrections are new rows**) ·
`recurring_events` · `debts` · `decisions` (the frozen snapshot + output + engine version)
· `payments` (the state machine, [5]) · `policies` (buffer, caps, blackouts).

Every table is scoped by `household_id`, enforced at the repository layer **and** by Postgres
row-level security. An IDOR here exposes someone's complete financial life; one forgotten
`WHERE` clause is not an acceptable single point of failure. Plaid access tokens are
envelope-encrypted with a KMS key, never a plaintext column beside everything else.

**The tenant is the household, not the user** — corrected from `user_id`, which this sketch
carried until 2026-07-16. Everything the product reasons about is a household: `sim/`,
`engine/`, [`prd.md`](./prd.md) §3, and [`USERS.md`](../USERS.md) all say so. A `user` is a
login, and one household may eventually have two of them — for this product that is not a
footnote, because a spouse's spending is precisely what breaks a forecast. `users` stays in
the sketch above as the login table, arriving with auth; it is not the scoping key.

### [4.1] Where the data lives, and when that changes

Three phases. Each transition is triggered by an **event, not a date or a user count** —
and one of the three triggers does not exist yet, which is said here rather than hidden.

| Phase | Store | Trigger to leave it |
|---|---|---|
| **1 — demo** | Postgres on **Neon**. Real RLS, real declarative partitioning, scale-to-zero. No Cloud SQL connector in the deploy path. | The first real household's data. |
| **2 — pilot** | Postgres on **Cloud SQL**. | **Not yet argued.** See below. |
| **3** | Not a database. See below. | — |

**Phase 1 is Neon because the demo's constraint is cold-start, not throughput.**
[`USERS.md`](../USERS.md) §2's reader wants to see this work in under a minute; Neon wakes in
~300–500ms and costs nothing at rest. It runs stock Postgres compute, so RLS and declarative
partitioning are ordinary features rather than emulations — which matters, because a demo
built on an RLS *substitute* would prove nothing about [7.1].

**Phase 1 → 2 is triggered by the first real household, not by a user count.** That is when
GLBA applies, when Plaid tokens exist, when the per-household DEK has something to protect,
and when "scales to zero" stops being a feature and becomes "can be cold when someone's rent
is due."

**But the trigger is the arrival of real data, not a limit of Neon's — and the difference
matters.** Neon is SOC 2 Type II with encryption at rest and in transit; nothing about real
household data exceeds it. The honest case for Cloud SQL is locality, not capability: the rest
of the stack is GCP ([2]), and KMS, Secret Manager, and Cloud Run sharing a trust boundary
with the database is worth something. **That is a real argument and it is not yet a made one.**

Read [`prd.md`](./prd.md) §2.4 before spending on this migration. Its whole subject is a
decision that survived because *no document ever argued for it* — a daily cadence inherited
from a product that no longer existed. "Postgres/Cloud SQL" has been in this document since v1
and has never been argued either. It may well be right. It should be **argued before it is
paid for**, and if the argument does not close, phase 2 is Neon and this table gets shorter.

**There is no phase 3 for the database, and the arithmetic is why.** The serving workload is
one decision per household per day — that is what §2.4's "daily data, weekly money" buys:

| At | Decisions/day | Avg write rate |
|---|---|---|
| 100K households | 100K | ~1.2/sec |
| 1M households | 1M | ~11.6/sec |
| 5M households | 5M | ~57.9/sec |

Fifty-eight writes per second is not a scaling problem, and 5M is already past the ceiling
[`prd.md`](./prd.md) §2.2's variance gate admits. Postgres is not the constraint at any user
count this product's own segment sizing allows. AlloyDB, read replicas, and a bigger instance
are **dials inside phase 2**, not a phase — and all three are wire-compatible.

**Storage does not run out either, and this was measured rather than assumed.** An earlier draft
of this section put ~10KB/snapshot and 18 TB/yr here on a **guess**, and then reasoned about a
phase 3 from it. [`prd.md`](./prd.md) §2.4 exists partly to warn about exactly that move — it
documents two plugged-in numbers that "pointed the right way for the wrong reason." This is the
correction.

Measured over 90 consecutive `Snapshot`s of the demo household, serialized with
[`artifact.py`](../backend/artifact.py)'s tagged-scalar scheme:

| | mean/snapshot | vs raw |
|---|---|---|
| raw JSON | **2,699 B** | — |
| gzip'd individually | 770 B | 3.5× |
| gzip'd as a batch | 38 B | **70.7×** |
| lzma'd as a batch | 25 B | 108× |

**The ratio is the finding, not the size.** Consecutive days for one household are nearly
identical — `today` moves, a few balances move, the rest is unchanged — so a store sorted by
`(household_id, day)` compresses ~70× while a store that compresses each value independently
gets ~3.5×. **Postgres TOAST is the second kind.** That is the `SnapshotStore` seam's real
justification, and it is a smaller one than "18 TB": it is roughly the difference between a
few hundred dollars a month and a few tens, at 5M households.

Scale it honestly and it does not threaten anything. The demo household is small — 2 accounts,
5 events, 1 card. A real one is bigger: [`USERS.md`](../USERS.md) says two or three cards, a
real recurring-event detector ([3.2]) emits far more than 5 events, and real accounts carry
pending transactions. Call it 3–5× raw, and call the compression 20–30× rather than 70× because
real balances jitter where synthetic ones do not. **That still lands under a TB/yr at 5M
households** — tens of dollars of object storage.

So the thing that looked like phase 3 **is not a different system of record, and on these
numbers it is not anything.** The seam still earns its keep for the reason above. The *trigger*
does not fire:

- **Trigger:** the backtest scan, not the serving path, becomes the constraint. **Measured, and
  it does not** — at sub-TB/yr this is a once-per-engine-version batch read, not an
  architecture.
- **Explicitly not:** a different OLTP store ([1.2]'s row on `FinancialProvider`), and not an
  analytics engine over the snapshots — see below.

**The backtest is not an analytics query.** It runs `engine.decide()` and `outcome.grade()` —
Python — over each snapshot. BigQuery, ClickHouse, and DuckDB cannot do that, so none of them
is a candidate for the scan; it is a parallel map (Cloud Run Jobs or Dataflow), and the
arithmetic is undramatic: 5M households × ~163ms measured per household-90-days is ~2 hours
across 100 workers.

**Where an analytics engine does earn a place is the graded output**, not the input.
[`strategy.md`](./strategy.md) §3's asset is "the empirical distribution of our own errors, by
user archetype, by pay cadence, by season" — that is an aggregation over `Outcome` rows, which
are ~10 numeric fields each and tens of GB/yr at 5M. That is a real and small BigQuery table.
**Not built, and not needed until shadow mode has real households to grade** ([`prd.md`](./prd.md)
§8.1).

**This still reverses [1.2]'s "Cloud Storage — nothing stores blobs"**, and the reversal is
recorded rather than quietly performed — just for a duller reason than the one first written
here.

---

## [5] Money movement (not yet built)

There is **no universal "pay this card" API.** Card networks are not a repayment rail;
each issuer decides what it accepts. Options, in increasing order of pain: deep-link
handoff → bill-pay partner → FBO/custodial account with a bank partner (heaviest
compliance; avoid as long as possible).

Never call `make_payment()` from a scheduled job against a live balance. The state machine
is the product:

```
proposed → authorized → submitted → pending → settled
                              ↘ returned / failed / cancelled
```

- **Idempotency key** derived from `(user, date, decision_id)`. A retry after a timeout
  must never double-debit — the highest-stakes idempotency in the system, because a
  duplicate sweep *is* an overdraft.
- **Pre-flight re-check immediately before submission.** The decision may be hours old:
  re-run the freshness gates. A decision is a proposal; authorization is a separate,
  fresher act.
- **Returns are normal** (R01 insufficient funds, R02 closed, R03 no account). A payment
  can unwind days after we told the user it happened. The explanation surface and the
  ledger must both be able to say *"this reversed."*
- **This is where Temporal earns its place** — durable execution, retries, compensation.
  Not before.

---

## [6] Build order

1. **Shadow mode.** Ingest → normalize → snapshot → decide → log. **Move nothing.** Compare
   each projected low balance against what actually happened. This produces the one thing
   no competitor in this category had before switching the money on: **a measured tail-risk
   number.** It is also what sets the engine's thresholds, which are currently judgment
   ([`decision-engine.md`](./decision-engine.md) §6.1).
2. **Sweep, small caps, guarantee live.** Overdraft reimbursement from the first dollar.
3. **Raise the ceiling** as calibration proves out — the cap is a function of demonstrated
   forecast calibration, never of a growth target.

---

## [7] Decisions to make before the first migration

Each of these changes the schema, so none can be deferred:

1. **Authz pattern** — repository-layer scoping + Postgres RLS, gated by an IDOR test suite.
2. **Plaid token storage** — KMS envelope encryption; rotation and revocation runbook tied
   to Plaid's item-error webhooks.
3. **GLBA + CCPA posture** — GLBA applies from day one regardless of money movement.
   CCPA/CPRA deletion rights are in direct tension with append-only storage; resolve the
   tension in the schema, not after it.
4. **Plaid production-access review** — a real launch gate that asks for exactly the above.
   Track it as a dependency, not a formality.
5. ~~**APR fallback**~~ — **settled 2026-07-16 by ticket `0028`: estimated, at 23%, with the
   confidence carried in `cards.apr_source` rather than implied.** The engine ranks on the
   estimate and [`decision-engine.md`](./decision-engine.md) §6.3 explains why that is safe
   (`APR_UNKNOWN` gates value, not safety); `engine/interest.py` refuses to compute a saving from
   one, so [`prd.md`](./prd.md) §5.1's KPI is never arithmetic on a guess. `USER_ENTERED` is in
   the enum and the schema but has no entry path — §6.3's "a real product needs a user-entered
   fallback" is still unbuilt.
