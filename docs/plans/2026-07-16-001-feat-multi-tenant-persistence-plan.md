---
title: Postgres scoped by household, and the households this engine has never seen
type: feat
status: active
date: 2026-07-16
origin: docs/brainstorms/2026-07-16-multi-tenant-persistence-and-seeded-households.md
adr: docs/decisions/0004-postgres-scoped-by-household.md
---

# Postgres scoped by household, and the households this engine has never seen

## Summary

Replace the generated JSON artifact with Postgres scoped by `household_id`, and seed it with
households that are **not** the demo household — because every household this engine has ever run
against is the same one.

The ask was "a database, seeded with a couple of customers, so I can understand the dashboards."
Scoping it found that the hard part is neither the database nor the seeding. `sim/` is already a
correct multi-household primitive and `build()` already takes `spec`/`seed`/`policy`; single-tenancy
lives in four places. What the scoping actually found was two defects and one absence:

1. **`calibrate.py` sweeps a dial `build()` cannot read** (U1). `assemble_snapshot()`'s docstring
   says it exists so `build()` and `replay()` do not drift. They have drifted. Invisible today only
   because the dial is off.
2. **Every household is biweekly with one card at 23.99%** — all 60 in the calibration population.
   So [`decision-engine.md`](../decision-engine.md) §9.3's admission that the spacing rule "is a
   decent approximation of a biweekly household and a poor one for everyone else" has never been
   tested, because there is no everyone else.
3. **There is no tenant concept anywhere** — zero occurrences of `household_id`, `tenant`, or
   `user_id` in `backend/`, `engine/`, `sim/`, or `mobile/src/`.

**This plan builds phase 1 only** ([`architecture.md`](../architecture.md) [4.1]). It does not build
Plaid, money movement, real auth, or a live daily decision job.

---

## Build Progress

*Last updated 2026-07-16. **U1–U4 are on `main`** (PR #44). **U5's two blockers are fixed**
(`0027`, `0028` — PR #46, open). 448 Python tests, 51 mobile tests, ruff clean, and
`backend/data/decisions.json` byte-identical throughout every one of them.*

| Unit | Ticket | Status | Landed in |
|------|--------|--------|-----------|
| U1 The walk, unified — and the dial nothing reads | `0019` | done | #44 |
| U2 Schema, RLS, and the first migration | `0020` | done | #44 |
| U3 Repository scoping and the IDOR suite | `0021` | done | #44 |
| U4 The `SnapshotStore` seam | `0022` | done | #44 |
| U5 The archetypes, and the seeder | `0023` | **unblocked, not started** | — |
| U6 The read path | `0024` | not started | — |
| U7 Mobile: the household switcher | `0025` | not started | — |
| U8 Neon, and the deploy path | `0026` | in progress | #44 (partial) |
| — A ledger per card *(spawned by U5)* | `0027` | done | #46 |
| — APR provenance *(spawned by U5)* | `0028` | done | #46 |

### What the build found, and what it cost to find

Every one of these is the same shape, and it is this plan's own thesis: **a mechanism that was
built, tested, and never actually exercised.** None was in the ticket that found it.

**Shipped in #44:**

- **`calibrate.py` swept a dial `build()` could not read** (U1). The defect the plan was written
  around. Fixed; the artifact is byte-identical and `calibrate.measure(None)` is unchanged at
  4,320 days / 2.338% / 0 overdrafts / $544,640.58.
- **Neon's `neondb_owner` has `rolbypassrls = true`** (U8). Scoped to one household it returned
  **both**. Pasting the connection string Neon hands you into `DATABASE_URL` — the obvious
  deployment — would have shipped U2 and U3's two layers as one, with the IDOR suite green the
  whole time. `assert_rls_binds()` now refuses to start under such a role.
- **The IDOR suite was vacuous** (U3). It ran as the superuser that owns the tables, which bypasses
  RLS even when FORCEd. Caught by its own `test_the_app_engine_is_not_secretly_a_superuser`.
- **`SET LOCAL` cannot take a bind parameter** (U2), so the obvious spelling forces a
  request-supplied value into the SQL string. `set_config(:var, :hid, true)` is the safe *and*
  correct one.
- **A role is cluster-wide; a migration is database-scoped** (U2). `downgrade()` failed naming a
  database the migration had never heard of. Two Neon branches would deadlock each other.
- **The mobile CI flake was two bugs, one hiding behind the other.** A cold babel transform
  (13.86s vs 2.38s warm) against jest's 5s default, *and* `Spending.test.tsx` calling `render()`
  bare where every other suite awaits it. The second fails identically at 5s and 120s — raising the
  timeout would have buried it. It had blocked three PRs, one of them documentation-only.

**Blocked U5; both now fixed in #46:**

- **The walk cannot simulate a multi-card household.** `walk()` builds one `DebtLedger` from
  `spec.card`, and `assemble_snapshot` hands that single balance to *every* card: a two-card
  household with a $14,000 and a $3,000 card reports **$14,009.20 for both**. `_select_target`
  would rank them equal and pick on APR alone; the reserve would count $14,009 twice.
  `HouseholdSpec.card`'s own docstring says reading it "on a two-card household is exactly the bug
  this feature exists to fix" — and the walk read it. **Fixed in `0027`**: a ledger per card, and
  it turned out to be *four* single-card assumptions, not one — the shared ledger, the scalar
  `ledger_balance`, `card_payments` keyed on day alone (which dropped `Txn.card_id` **and**
  collided), and `DebtLedger` posting on a module constant rather than each card's close day.
  With it, `_select_target`'s ranking chose between two real cards for the first time.
- **`CardSpec.apr` is `Decimal`, not `Decimal | None`**, so archetype D was inexpressible.
  **Settled in `0028`**, which also closes [`architecture.md`](../architecture.md) §7.5: estimate
  at **23%**, carry the provenance in `AprSource`, let `decide.py` **rank** on the guess, and make
  `interest.py` **refuse to price** it. *Act on the estimate; never bill for it.* The override of
  §6.3 is defensible because **`APR_UNKNOWN` is not a safety gate** — a wrong target optimizes
  worse and overdraws nobody, so `prd.md` §5.2's guardrail cannot move, and `calibrate` confirms
  it did not. `CardSpec.apr_reported` rather than a nullable APR: the card *has* a rate, Plaid
  merely does not report it.

**Open from U1, deliberately deferred:**

- **`sim.household.generate()` is not prefix-stable.** `(spec, seed, days=150)` and
  `(spec, seed, days=181)` are different households, so `build()` (150) and `replay()` (181) have
  never walked the same one — U1's thesis one level deeper. `calibrate.py`'s **population**
  statistics survive; **per-household** claims do not. Fixing it regenerates the artifact and moves
  every measured number in three documents, so it needs its own ticket and its own measurement.

---

## Problem Frame

### The defect that reorders the work

`assemble_snapshot()` (`backend/precompute.py:789-850`) has a docstring at lines 803-812 saying it
is exported for `backend/replay.py` **so the two do not drift.** Then:

- `build()`'s loop does not call it — it builds a `Snapshot` inline at `backend/precompute.py:943-973`.
- That inline `Snapshot` never sets `spend_30d_high`, so it defaults to `None` (`engine/models.py:633`).
- `assemble_snapshot()` sets it: `spend_30d_high=spend_30d_high(history, today, spend_quantile)`
  (`backend/precompute.py:845`).
- `build()` has **no `spend_quantile` parameter at all** (`backend/precompute.py:863-870`).
  `replay()` has one (`backend/replay.py:159`).
- `calibrate.measure(quantile)` (`backend/calibrate.py:136`) sweeps 9 settings through
  `replay()` → `assemble_snapshot()`.

Harmless today: `SPEND_QUANTILE = None` (`backend/precompute.py:123`), so both produce `None`.

**Not harmless the day the dial moves — which is the only reason `calibrate.py` exists.** If it
ever licenses a setting, it will have measured a forecast `build()` is structurally incapable of
shipping. The harness would grade an engine that is not the one serving. That is
[`decision-engine.md`](../decision-engine.md) §8.4's bug class exactly, and §6.6's test guards the
*measurement*, not the *wiring*.

The ledger/settlement bookkeeping is separately copy-pasted character-for-character between
`backend/precompute.py:886-890` and `backend/replay.py:200-202`.

**So there are 2.5 copies of the walk and they have already drifted once. A seeder must not become
the fourth.** That is why U1 leads and why this is a refactor rather than an addition.

### The absence the seed data fills

`DEMO_SPEC` is biweekly, one card, 23.99% APR. `calibrate._spec_for()` (`backend/calibrate.py:82-84`)
is `replace(DEMO_SPEC, spend=charging)` — it varies **only** the spend shape. The 60-household
population is 3 spend shapes × 20 seeds of **one archetype**.

`PayCadence.WEEKLY`/`SEMIMONTHLY`/`MONTHLY` are exercised nowhere outside `sim/`'s own `_paydays`
and `tests/test_household.py:215`. Multi-card `cards=(...)` tuples appear only in tests — despite
`sim/household.py:229-231` saying the tuple exists because "a portfolio reserve and a coverage gate
need more than one card to have anything to bite on."

This is [`prd.md`](../prd.md) §5.2's lesson one level up: *"a guardrail measured on one household is
not measured."* Sixty households deep on one axis is one household on every other axis.

---

## Resolved Decisions

All in [ADR-0004](../decisions/0004-postgres-scoped-by-household.md) and
[`architecture.md`](../architecture.md) [4.1]; summarized so this plan reads alone.

| | Decision |
|---|---|
| **Store** | Postgres, on **Neon** through the demo. Cloud SQL at pilot — trigger exists, argument does not, and is not this plan's to spend. |
| **Tenant** | `household_id`, not `user_id`. A `user` is a login; a household is what the product reasons about. |
| **Snapshots** | JSONB behind a two-method `SnapshotStore`; `decisions.snapshot_ref` is opaque. Justified by a measured **3.5× vs 70×** compression gap, **not** volume. |
| **Partitioning** | `decisions` and `transactions` by month, from the first migration. Free now, a scheduled migration later. |
| **Deletion** | Crypto-shredding. `dek_id` column now; KMS wiring lands with Plaid, when there is PII. |
| **Money** | Postgres `NUMERIC` — exact natively. The `$dec` codec survives **only** for the JSONB snapshot payload. |
| **Why now** | **Reversibility, not scale.** The arithmetic says Postgres was never in danger (~58 writes/sec, sub-TB/yr at 5M) and [`prd.md`](../prd.md) §2.2 disputes "millions" anyway. |

---

## Key Technical Decisions

**The `$dec` codec is promoted, not deleted.** `backend/artifact.py`'s `_encode`/`_decode`
(lines 78-107) is exactly what `SnapshotStore` needs — the snapshot payload is JSON and still has no
decimal type. It is already tested. It moves; it does not get rewritten.

**`Artifact`/`DayRecord`/`Summary`/`SpendSnapshot` are response shapes, not persistence.** They
survive U6 built from rows instead of from a file. This is a migration of the *source*, not a
rewrite of the API.

**RLS is scoped with `SET LOCAL` inside a transaction, never `SET`.** Neon pools connections. A
plain `SET app.household_id` leaks across a pooled connection and is an IDOR wearing a security
feature's clothes. See Risks.

**CI runs a `postgres:16` service container; Neon is the deployed demo.** CI must not depend on an
external instance for secrets, cost, or flakiness. Neon is stock Postgres, so the drift is nil.

---

## High-Level Technical Design

```
sim.household.generate(spec, seed) ──> History
                                          │
                            ┌─────────────┴──────────────┐   U1: ONE walk
                            │   walk(history, spec, ...)  │   yields (day, Snapshot, Decision, ledger)
                            └─────────────┬──────────────┘
                                          │
                 ┌────────────────────────┼────────────────────────┐
                 │                        │                        │
            build()                  replay()                   seed()          U5
       (Artifact — fixture)      (Graded — calibrate)      (rows — repository)
                                                                   │
                                                          ┌────────┴────────┐
                                                          │  Repository     │  U3
                                                          │  SET LOCAL RLS  │
                                                          └────────┬────────┘
                                                                   │
                                          ┌────────────────────────┴──────────┐
                                          │  Postgres (Neon)                  │  U2
                                          │  households/accounts/cards/       │
                                          │  policies/decisions[part]/        │
                                          │  snapshots  ← SnapshotStore  U4   │
                                          └───────────────────────────────────┘
```

### The schema

```sql
households (id, archetype, dek_id, created_at, deleted_at)
accounts   (id, household_id FK, kind, balance NUMERIC, connection, balance_age_days)
cards      (id, household_id FK, apr NUMERIC NULL, statement_balance NUMERIC,
            statement_due_date, minimum_payment NUMERIC, unbilled_balance NUMERIC,
            next_close_date, behavior, observed_monthly_payment NUMERIC NULL, ...)
policies   (household_id FK, buffer_floor NUMERIC, max_sweep NUMERIC,
            max_weekly_sweep NUMERIC, min_days_between_sweeps INT, blackout_dates DATE[])
decisions  (id, household_id FK, day, action, amount NUMERIC, target_card_id,
            projected_low_balance NUMERIC NULL, reasons JSONB, engine_version,
            snapshot_ref TEXT)                        PARTITION BY RANGE (day)
snapshots  (id, household_id FK, payload JSONB)       -- reached only via SnapshotStore
```

`reasons` stays queryable JSONB — refusal-rate metrics slice it. The snapshot payload does not; it
is opaque behind the seam.

**Not created:** `items`, `transactions`, `recurring_events`, `payments`, `users`. Named in
[`architecture.md`](../architecture.md) [4]; shape not yet known. Empty tables invite guessed
columns.

---

## Implementation Units

### U1. The walk, unified — and the dial nothing reads

**Ships alone. No database. Fixes a live defect.**

Extract the day-stepping loop from `build()` (`backend/precompute.py:882-996`) into a generator
yielding `(day, Snapshot, Decision, ledger_state)`. `build()` becomes a thin wrapper that assembles
`DayRecord`/`Summary`/`SpendSnapshot` on top (`backend/precompute.py:997-1007`, `1010-1063`).
`replay()` drives the same generator and grades. The inline `Snapshot` at 943-973 is deleted;
`assemble_snapshot()` becomes the only one, as its docstring always claimed. `build()` gains
`spend_quantile`.

**Files:** `backend/precompute.py`, `backend/replay.py`, `tests/test_precompute.py`,
`tests/test_replay.py`, `docs/decision-engine.md` (§6.6's dial note)

**Verification — this is the whole unit:**
- `tests/test_precompute.py:489-494` (byte-identical committed artifact) stays green **without
  being regenerated.** Regenerating it to match new output deletes the only evidence the refactor
  preserved behavior. If it goes red, the refactor is wrong — not the file.
- **New test, the one that would have caught this:** for a given `(spec, seed, day)`, `build()`'s
  snapshot and `replay()`'s snapshot are equal — at `spend_quantile=None` **and** at a non-`None`
  setting. That second case fails on today's code.
- `tests/test_precompute.py:279-281` (determinism) stays green.

### U2. Schema, RLS, and the first migration

Alembic; the schema above; `NUMERIC` for every money column; `PARTITION BY RANGE (day)` monthly on
`decisions`; RLS policies on every household-scoped table keyed off `current_setting('app.household_id')`.
`postgres:16` service container in CI.

**Files:** `backend/db/models.py`, `backend/db/session.py`, `alembic/`, `pyproject.toml`
(`psycopg[binary]`, `sqlalchemy`, `alembic`), `.github/workflows/`, `tests/test_schema.py`

**New dependencies, justified:** SQLAlchemy Core + Alembic. ADR-0002 celebrated "no ORM" — this uses
Core, not the ORM, so the SQL stays visible and the migration story is real. Alembic because a schema
without migrations is a schema that drifts.

**Verification:** migration up/down clean; a money column round-trips a `Decimal` exactly; partitions
are created and routed to.

### U3. Repository scoping and the IDOR suite

[`architecture.md`](../architecture.md) §7.1's pattern: repository-layer scoping **and** RLS, gated
by an IDOR suite. `SET LOCAL app.household_id` inside a transaction.

**Files:** `backend/db/repository.py`, `tests/test_idor.py`

**Verification:** for **every** household-scoped table, a repository scoped to household A cannot
read, update, or delete a row belonging to B — asserted at the repository layer *and* with the
repository bypassed, so RLS is proven independently rather than by proxy. A test that the session
variable does not survive a returned connection.

### U4. The `SnapshotStore` seam

```python
class SnapshotStore(Protocol):
    def put(self, household_id: str, day: date, payload: bytes) -> str: ...
    def get(self, ref: str) -> bytes: ...
```

`PostgresSnapshotStore` writes the `snapshots` table and returns `"pg:<id>"`. Payload encoded with
`artifact.py`'s promoted `$dec` codec.

**Files:** `backend/db/snapshots.py`, `backend/artifact.py` (extract the codec), `tests/test_snapshots.py`

**Verification:** a `Snapshot` round-trips **exactly** — every `Decimal` identical, no float anywhere
in either direction. `ref` is opaque to callers (a test that nothing parses it).

### U5. The archetypes, and the seeder

Four `HouseholdSpec`s; the seeder drives U1's generator and writes through U3's repository.

| # | Archetype | Cadence | Cards | Stresses |
|---|---|---|---|---|
| A | `demo_biweekly` | biweekly | 1 @ 23.99% | **Regression anchor.** Today's `DEMO_SPEC`, unchanged. Its decisions must not move. |
| B | `semimonthly_portfolio` | semimonthly | 3, mixed APR + behavior | `_select_target` ranking; the portfolio reserve; §9.3's mismatch |
| C | `monthly_thin` | monthly | 2 | **§9.3 at its worst** — one payday vs ~4.3 sweep windows/mo |
| D | `apr_unknown` | biweekly | 2, both `apr=None` | `APR_UNKNOWN` (`engine/decide.py:199-204`) |

**D needs two cards.** `_select_target` (`engine/decide.py:172-206`) uses a lone card's missing APR
without complaint and only refuses when *every* targetable card lacks one. A single APR-less card is
a different state worth seeing: a normal sweep with no `INTEREST_AVOIDED` reason, because
`interest.py:115-116` returns `None` rather than invent a number.

**Files:** `backend/seed.py`, `backend/archetypes.py`, `tests/test_seed.py`

**Verification:**
- Re-seeding produces **identical rows** — ADR-0002's staleness property, re-pointed.
- Archetype A's decisions are byte-identical to the committed artifact's.
- **Run `calibrate` across the new archetypes and report the numbers.** Do **not** silently
  re-baseline: [`prd.md`](../prd.md) §5.2/§5.3's headline figures (2.3% breach, 0 in 590, ~$544K)
  were measured on the old population and are cited in three documents.

### U6. The read path

`main.py` serves from the database. `GET /households`; `GET /households/{id}/decisions`, `/spend`,
`/decisions/{day}/explain`. `app.state.artifact` is gone.

**Auth stays the shared key plus explicit household selection, flagged as a dev posture, not a
production one.** Seeded synthetic households have no owner to authenticate as. Clerk lands with
Plaid. The *mechanism* (U3) is real and tested; the *identity* is not.

**Files:** `backend/main.py`, `backend/auth.py`, `backend/artifact.py`, `USERS.md`,
`tests/test_main.py`

**Verification:** every route requires a household; the demo household's `/decisions` response is
unchanged from today's; `backend/data/decisions.json` is no longer read at runtime (a test asserts
`main.py` does not load it).

### U7. Mobile: the household switcher

A picker; the client threads `household_id`. This is the unit that delivers the actual ask — seeing
the dashboards against households that differ.

**Files:** `mobile/src/api/client.ts`, `mobile/src/api/types.ts`, `mobile/src/screens/`,
`mobile/src/components/`, tests

**Verification:** switching re-fetches and re-renders; the Spending screen follows the switch.

### U8. Neon, and the deploy path

Neon project; connection string in Secret Manager; migrations in the deploy path; `Procfile`
unchanged.

**Files:** `DEPLOY.local.md`, `.github/workflows/`, `Procfile` (if needed)

**Verification:** cold-start the deployed demo and confirm it serves inside
[`USERS.md`](../../USERS.md) §2's under-a-minute bar. Neon wakes in ~300-500ms; measure it rather than
trust the brochure.

### Dependency order

```
U1 (walk)  ──────────────────────────────┐
U2 (schema) ──┬── U3 (repo + IDOR) ──┐   │
              ├── U4 (snapshots) ────┼───┴── U5 (seed) ── U6 (read path) ── U7 (mobile)
              └── U8 (Neon/deploy)   │
```

U1 and U2 are independent and can run in parallel. **U1 ships first regardless** — it fixes a live
defect and needs nothing.

---

## Scope Boundaries

### Explicitly NOT in this plan

- **Plaid.** This builds the tenancy Plaid needs, not Plaid.
- **Money movement.** [`prd.md`](../prd.md) §6.1: no rail is chosen and "nothing in the codebase
  assumes one — keep it that way." §7.1: distribution forecloses it and is first in sequence.
- **The live daily decision job.** No live input until Plaid. The seeder exercises the write path.
- **Real authentication.** See U6.
- **Real KMS wiring.** `dek_id` lands; the key does not. No PII exists yet.
- **Object storage for snapshots.** The seam makes it a swap. Measured as unnecessary — see
  [`architecture.md`](../architecture.md) [4.1].
- **An analytics engine.** The backtest runs `engine.decide()` — it is a parallel Python map, not a
  query. The graded *output* is where one earns a place, and that needs shadow mode first.
- **Fixing `derive_cash_events`'s spec-reading.** It reads `HouseholdSpec`, not `History`
  (`backend/precompute.py:294`) — the recurring-event detector
  [`decision-engine.md`](../decision-engine.md) §6.2 assumes away. Plaid's wall. Every household
  here has a spec by construction.
- **Any engine change.** `engine/` and `sim/` keep `dependencies = []`.
- **Loosening the income-variance gate.** [`prd.md`](../prd.md) §2.2 makes it disqualifying and
  [`strategy.md`](../strategy.md) §4 says it is the lever that destroys the product.

### Deferred to follow-up

- Cloud SQL (phase 2) — and the argument it still lacks.
- The cash-cycle spacing rule [`decision-engine.md`](../decision-engine.md) §9.3 wants. Archetypes B
  and C will price it. **Pricing it is not fixing it** — the fix needs the recurring-income detector.

---

## Risks

| Risk | Mitigation |
|---|---|
| **`SET` instead of `SET LOCAL` leaks a tenant across a pooled connection.** Neon pools. This is an IDOR wearing a security feature's clothes, and it would pass a naive IDOR suite that reuses one connection. | `SET LOCAL` inside a transaction, always. U3's suite explicitly tests that the session variable does **not** survive a returned connection. |
| **The walk refactor breaks the artifact.** Over half of `tests/test_precompute.py` (~262-776) calls `build()` and expects an `Artifact`. | `build()` stays a thin wrapper. The byte-identical test is the safety net and must stay green **without regeneration**. |
| **Archetypes B/C/D refuse constantly**, producing honest but useless dashboards. | That is a finding — §9.3's cost made visible — not a bug. Measure it with `calibrate` before anyone concludes the engine is broken or, worse, "fixes" it by loosening a gate. |
| **The new archetypes change the calibration population**, and §5.2/§5.3's headline numbers were measured on the old one and cited in three documents. | Do not silently re-baseline. Re-measure, and state the change. |
| **RLS is reassuring in a way a shared key does not earn** — any key may read any household. | Flag the dev posture in `USERS.md` and U6, not only in code comments. |
| **Scope creep into Plaid**, since the schema is shaped by data Plaid will supply. | Plaid tables are named in [`architecture.md`](../architecture.md) [4] and **not created**. |
| **Neon cold start pushes the demo past `USERS.md` §2's minute.** | ~300-500ms claimed. U8 measures it. If it misses, that is a phase-2 trigger arriving early — not a reason to keep the artifact. |

---

## Verification Strategy

Per unit, above. Across the plan:

- **`pytest`, `ruff`, and the mobile typecheck + tests** — the existing gate (`.github/workflows/`).
- **The two properties ADR-0002 enforced in code, re-pointed:** exact `Decimal` round-trips (now
  `NUMERIC`, plus the `$dec` codec for the payload) and determinism-as-a-test (re-seed → identical
  rows).
- **The regression oracle:** archetype A's decisions are byte-identical to today's committed
  artifact, through U1's refactor and U5's seeder both.
- **`python -m backend.calibrate` across the new archetypes**, reported — not re-baselined.

---

## Review History

*None yet. This plan is a candidate for `/ce-doc-review` before U1 starts — the walk refactor is the
kind of change the card-spend plan's own history shows benefits from adversarial review before code
exists.*
