# Multi-tenant persistence, and the households this engine has never seen

**Date:** 2026-07-16 · **Status:** scope, not yet planned · **Feeds:** an ADR superseding
[`0002`](../decisions/0002-generated-json-artifact-over-database.md), then a plan doc, then tickets
**Touches:** `backend/precompute.py`, `backend/replay.py`, `backend/artifact.py`, `backend/main.py`,
`backend/auth.py`, a new `backend/db/`, `mobile/`. **`engine/` and `sim/` are not touched.**

**Fact-checked against the code on 2026-07-16.** Every structural claim below was verified line by
line. Two of them are defects that were not in the ask and are not in any existing doc.

---

## [0] What this actually is

The ask was *"build out a database structure and seed it with mock data for a couple of different
customers so I can better understand the dashboards."* Scoping it turned up six findings. Two are
defects, and one of those is the reason this work should unify code rather than add to it.

### [0.1] The simulator is already a correct multi-household primitive

Nothing needs to be built to *make* households. `sim.household.generate(spec, start, days, seed)`
(`sim/household.py:435`) is a pure function of its arguments, and `HouseholdSpec`
(`sim/household.py:223-250`) already carries every axis that distinguishes one household from
another: `PayrollSpec.cadence`, `bills`, `SpendSpec`, and a **tuple** of `CardSpec`.

`backend/precompute.py:863-870`'s `build()` already accepts `spec`, `start`, `warmup_days`,
`served_days`, `seed`, and `policy`. `backend/calibrate.py:136-147` already stands up 60 households
by calling `replay(spec=..., seed=...)` in a loop.

**The multi-household machinery exists. Only the serving layer is single-tenant.** This finding is
what makes the ask small.

### [0.2] Every household in this repo is the same household

`DEMO_SPEC` (`backend/precompute.py:173-226`) is biweekly, one card, 23.99% APR, $14,000.

The 60-household calibration population does not vary that. `_spec_for()`
(`backend/calibrate.py:82-84`) is:

```python
charging = replace(shape, card_share=DEMO_SPEC.spend.card_share)
return replace(DEMO_SPEC, spend=charging)   # everything else held constant
```

So the population is **3 spend shapes × 20 seeds of one archetype**. Payroll, bills, and cards are
`DEMO_SPEC`'s in all 60.

`PayCadence.WEEKLY`, `SEMIMONTHLY`, and `MONTHLY` exist in the enum (`sim/household.py:57-61`) and
are exercised nowhere outside `sim/household.py`'s own `_paydays` internals and a single test
(`tests/test_household.py:215`). Multi-card `cards=(...)` tuples appear only in tests —
`HouseholdSpec`'s own comment (`sim/household.py:229-231`) says the tuple exists because "a
portfolio reserve and a coverage gate need more than one card to have anything to bite on," and
then no demo household has more than one.

### [0.3] So `decision-engine.md` §9.3's confession has never been tested

§9.3 already says it, in these words:

> A fixed 7-day spacing is a decent approximation of a biweekly household and a poor one for
> everyone else.

**There is no everyone else.** Every household the engine has ever run against is the biweekly one
the rule approximates well. §9.3 goes further and names the better design — decide once per *cash
cycle*, which for a monthly earner is once a month — and notes that `sim/household.py` already
models the cadence while the engine does not look at it.

This is `prd.md` §5.2's lesson one level up. §5.2 says *"a guardrail measured on one household is
not measured"* after a bug that a single seed hid and 60 households caught. Sixty households deep on
one axis is one household on every other axis.

**So the seed data is not a demo nicety.** It is the first pressure ever applied to a rule the
engine's own contract documents as wrong.

### [0.4] `calibrate.py` sweeps a dial that `build()` structurally cannot read

**This is a defect, it is live today, and it is invisible only because a dial is off.**

`assemble_snapshot()`'s docstring (`backend/precompute.py:803-812`) says it is exported for
`backend/replay.py` **so the two do not drift.** The drift it exists to prevent has already
happened:

- `build()`'s own loop **does not call `assemble_snapshot()`**. It constructs a `Snapshot` inline
  (`backend/precompute.py:943-973`).
- That inline `Snapshot` **never sets `spend_30d_high`**, so it falls back to the dataclass default
  `None` (`engine/models.py:633`).
- `assemble_snapshot()` **does** set it: `spend_30d_high=spend_30d_high(history, today,
  spend_quantile)` (`backend/precompute.py:845`).
- `build()` has **no `spend_quantile` parameter at all** (`backend/precompute.py:863-870`).
  `replay()` has one (`backend/replay.py:159`) and threads it through.
- `calibrate.measure(quantile)` (`backend/calibrate.py:136`) sweeps candidate quantiles through
  `replay()` → `assemble_snapshot()`.

Today this is harmless: `SPEND_QUANTILE = None` (`backend/precompute.py:123`), so both paths produce
`None` and agree.

**It stops being harmless the moment the dial moves — which is the entire purpose of
`calibrate.py`.** That file exists to find the right setting for this dial. If it ever licenses one,
`calibrate.py` will have measured a forecast that `build()` is structurally incapable of shipping.
The harness would be grading an engine that is not the one serving.

This is exactly the bug class `decision-engine.md` §8.4 and `prd.md` §5.2 celebrate catching. §6.6
notes that "a test fails if anyone moves it without a measurement" — that test guards the
*measurement*. Nothing guards the *wiring*.

### [0.5] Single-tenancy lives in exactly four places

1. `precompute.main()` (`backend/precompute.py:1090-1094`) calls `build()` with no arguments and
   writes one file.
2. `Artifact` (`backend/artifact.py:405-414`) has no household identifier in its schema.
3. `main.py` loads one artifact into `app.state.artifact` at startup (`backend/main.py:63`); every
   route reads it via `ArtifactDep` (`backend/main.py:85-92`). No request selects *whose* data.
4. `auth.py` is a single shared API key (`backend/auth.py:56-67`), described in its own docstring as
   "a lock on a door, not an identity system," and shipped inside the mobile bundle.

Grep confirms zero occurrences of `household_id`, `tenant`, or `user_id` anywhere in `backend/`,
`engine/`, `sim/`, or `mobile/src/`.

### [0.6] The derivation layer reads the generative spec — and the repo already knows

`derive_cash_events(spec, today, horizon)` (`backend/precompute.py:294`) takes a `HouseholdSpec`,
not a `History`. Its docstring is honest about it:

> A shortcut, and a deliberate one: the recurring-event detector `architecture.md` §3.2 describes
> does not exist yet, so rather than block this demo on building one, the events are read straight
> off the spec that generated the household. The engine is not being handed anything it could not in
> principle have learned from the transaction history — just handed it earlier, **and without the
> detector's errors.**

That last clause is the whole thing. A synthetic household has a spec; a real one does not. This is
the wall Plaid work hits, not this work — every seeded household here has a spec by construction.
**It is named here only so this plan does not make it worse**, and because it is the honest answer
to "why can't we just point this at a real bank tomorrow."

It also sharpens `prd.md` §8.1's caveat. §8.1 says the population "was generated by the same
assumptions the engine forecasts with." It is stronger than that: for recurring events, it is the
same *object*.

---

## [1] What we build

A real persistence layer, scoped by household, seeded from `sim/` with archetypes chosen to stress
the engine rather than to decorate a screen — and a single unified walk that `build()`, `replay()`,
and the seeder all drive, so [0.4] closes structurally instead of by convention.

---

## [2] Resolved decisions

### [2.1] Postgres, and the arithmetic that settles it

The workload is **one decision per user per day** — that is what `prd.md` §2.4 bought when it
spaced sweeps weekly while keeping the forecast daily ("daily data, weekly money").

| At | Decisions/day | Avg write rate |
|---|---|---|
| 100K households | 100K | ~1.2/sec |
| 1M households | 1M | ~11.6/sec |
| 5M households | 5M | ~57.9/sec |

Twelve writes per second is not a scaling problem. A horizontally-partitioned store would buy
throughput we do not need and cost three things we depend on: Postgres RLS, which
`architecture.md` §4 leans on as the defense against an IDOR that "exposes someone's complete
financial life"; cross-entity transactions, needed the moment a decision, a payment, and a policy
read must agree; and decimal exactness, which is the entire subject of ADR-0002's point 2 and of
`money()`'s refusal to accept a float.

**Storage does not threaten it either — measured, not assumed.** An earlier draft of this doc put
~10KB/snapshot here on a guess. A real `Snapshot` serialized with `artifact.py`'s scheme is **2,699
bytes** (demo household, 90 consecutive days), and the load-bearing number is not the size but the
**70× compression** from sorting by `(household_id, day)`: consecutive days are nearly identical.
Scaled honestly for a real household (3–5× bigger, 20–30× compression rather than 70×), it lands
**under a TB/yr at 5M households.** Full working and the correction in `architecture.md` [4.1].

Cloud SQL → AlloyDB remains a wire-compatible upgrade path if one is ever needed.

### [2.2] Frozen snapshots go behind a seam, stored as JSONB today

`architecture.md` §3.3 calls the frozen snapshot "the load-bearing design choice in the system" and
buys three things with it: explanation, audit, and backtest. Explanation and audit are point lookups
and Postgres is ideal. Backtest is a full-population scan.

**The seam's justification is compression, not volume** — and that is a smaller claim than the one
this doc made first. Postgres TOAST compresses each JSONB value independently (~3.5×); a store
sorted by `(household_id, day)` compresses ~70× because consecutive days barely differ. At 5M
households that is roughly the difference between a few hundred dollars a month and a few tens.
Real, worth a two-method seam, and **not** an architecture emergency.

So: `decisions` carries an opaque `snapshot_ref`, reached only through a two-method
`SnapshotStore`. Backed by a Postgres JSONB table today; movable to object storage without touching
a caller.

Two things to say honestly about this. It **reverses `architecture.md` §1.2**, which cut Cloud
Storage because "nothing stores blobs" — true at one household, false at a million. And a reviewer
will fairly ask whether this is the same mistake §1.2 warned about with `FinancialProvider` ("you
cannot design the seam from n=1"). It is not: a snapshot store is `put(bytes) → ref` and
`get(ref) → bytes`, one known shape with one known consumer, not a speculative abstraction over two
vendors we have never called.

**`decisions` and `transactions` are partitioned by month from day one.** At zero rows that is free;
at 365M rows it is a migration scheduled around.

### [2.3] Crypto-shredding resolves CCPA against append-only

`architecture.md` §7.3 flags the tension and says to "resolve it in the schema, not after it."
One data-encryption key per household in KMS; PII columns and snapshot payloads encrypted under it;
a deletion request destroys the key. Rows remain, bytes become unrecoverable, the append-only audit
trail holds, and decision **counts** still aggregate for calibration — so erasure does not punch
holes in the population that `prd.md` §5.2 says must be measured population-wide.

It also composes with the Plaid token envelope encryption `architecture.md` §7.2 already requires.

**Decided now, wired when there is a real secret.** The schema carries `dek_id` from the first
migration because that is the part that is expensive to retrofit. Actual KMS integration lands with
Plaid, when there is PII worth encrypting — synthetic households have none. This is
`architecture.md` §1.1's principle 5, not a deferral of the decision.

### [2.4] The seed archetypes are chosen to stress the engine

Per [0.2], the axes exist and are unused. Four households, and the count is a dial:

| # | Archetype | Cadence | Cards | What it stresses |
|---|---|---|---|---|
| A | `demo_biweekly` | biweekly | 1 @ 23.99% | **Regression anchor.** Today's `DEMO_SPEC`, unchanged. Its decisions must not move. |
| B | `semimonthly_portfolio` | semimonthly | 3, mixed APR + behavior | `_select_target` ranking; the portfolio reserve; §9.3's mismatch (2 paydays vs ~4.3 sweep windows/mo) |
| C | `monthly_thin` | monthly | 2 | **§9.3 at its worst** — one payday against ~4.3 sweep windows/mo. The household the spacing rule is documented to serve badly. |
| D | `apr_unknown` | biweekly | 2, both `apr=None` | The `APR_UNKNOWN` refusal (`engine/decide.py:199-204`) |

**D needs two cards, not one.** `_select_target` (`engine/decide.py:172-206`) uses a lone card's
missing APR without complaint — "it costs us nothing" — and only refuses when *every* targetable
card lacks one. A single APR-less card produces a different and also-worth-seeing state: a normal
sweep with no `INTEREST_AVOIDED` reason at all, because `interest.py:115-116` returns `None` rather
than invent a number. That is a dashboard that says "we moved your money and cannot tell you what it
saved," and it is worth looking at before a real issuer forces it.

### [2.5] The walk is unified, not copied

There are already **2.5 copies** of the walk, and per [0.4] they have already drifted:

- `build()`'s inline `Snapshot` (`backend/precompute.py:943-973`)
- `assemble_snapshot()` (`backend/precompute.py:789-850`), used only by `replay()`
- the ledger/settlement bookkeeping, **copy-pasted character-for-character** between
  `backend/precompute.py:886-890` and `backend/replay.py:200-202`

A seeder must not become the fourth. The walk becomes one generator yielding
`(day, Snapshot, Decision)` plus the ledger state a consumer needs; `build()` stays a thin wrapper
that assembles `DayRecord`/`Summary`/`SpendSnapshot` on top of it, `replay()` grades it, and the
seeder writes it. This keeps `tests/test_precompute.py`'s byte-identical regeneration test
(`tests/test_precompute.py:489-494`) meaningful as the refactor's own safety net.

**This is the argument for doing the refactor inside this work rather than bolting a seeder
alongside it.** It is not incidental cleanup — it closes a live drift.

---

## [3] What this supersedes

ADR-0002 said a second household "would require a real database, and this decision would be
superseded rather than extended — the artifact is regenerated wholesale, has no write path, and
holds everything in memory."

It called its own conditions exactly right. **A new ADR supersedes it**, and should say so in those
terms: the conditions ADR-0002 named have been met, not that ADR-0002 was wrong. It was right for a
single-tenant read-only demo and stayed right for three days of product decisions.

---

## [4] Scope boundaries

**Explicitly not in this plan:**

- **Plaid.** Item 2 of the roadmap. This plan builds the tenancy Plaid needs, not Plaid.
- **Money movement.** Item 3. `prd.md` §6.1 says no rail is chosen and "nothing in the codebase
  assumes one — keep it that way"; §7.1 says distribution forecloses that decision and is "first in
  sequence."
- **The live daily decision job.** There is no live input until Plaid. The seeder exercises the
  write path in the meantime, through the same repository the daily job will use.
- **Real authentication.** See [5.2].
- **Loosening the income-variance gate.** `prd.md` §2.2 makes it disqualifying. It is the one growth
  lever `strategy.md` §4 says destroys the product.
- **Fixing `derive_cash_events`'s spec-reading** ([0.6]). It needs the recurring-event detector
  `decision-engine.md` §6.2 lists as assumed away. Plaid's problem, named here so it is not
  rediscovered.
- **Any engine change.** `engine/` and `sim/` keep `dependencies = []`.

---

## [5] Open questions

### [5.1] The justification that goes in the ADR

The stated driver for choosing Postgres now is "scale to millions of users." Two committed documents
sit against that phrasing and the ADR has to reconcile them honestly, because this repo's whole
practice is catching exactly this:

- **`prd.md` §2.2**, verbatim: *"Segment sizing should assume roughly 15–20% of consumers, not
  'millions' unqualified."* The number that survives the variance gate is plausibly low single-digit
  millions, so "millions" holds as a **qualified ceiling** — but §2.2's point stands that it is not
  the unqualified market, and the arithmetic in [2.1] does not change either way.
- **`USERS.md` §2** says this repo's actual audience is "someone evaluating this repo as evidence of
  engineering judgment for a founding engineer role." An ADR justifying RLS and partitioning by
  "millions of users" over a population of 60 synthetic households invites the obvious question.

The defensible version of the ADR does not lean on the user count at all. It leans on **which
decisions are expensive to reverse** — partitioning, the snapshot seam, the `dek_id` column — and
builds exactly those, deferring every piece of operational apparatus (real GCS, real KMS, replicas)
until something real needs it. That argument is true at 60 households and at 5M, which is why it is
the one to make.

**Needs a call before the ADR is written.**

### [5.2] Authentication identity vs. the authorization mechanism

Seeded synthetic households have no owner to authenticate as, and `architecture.md` names Clerk for
a signup flow that does not exist. But `architecture.md` §7.1 wants "repository-layer scoping +
Postgres RLS, gated by an IDOR test suite," and **that mechanism is testable without real auth**: an
IDOR suite asserts a repository scoped to household A cannot return household B's rows, whatever
supplied the identity.

Proposal: build the mechanism (RLS via a session variable, repository scoping, the IDOR suite) and
let the identity source stay the shared key plus an explicit household selection — **flagged loudly
as a dev posture, not a production one.** Clerk lands with Plaid, when a real user signs up. The
risk to name: an IDOR suite is reassuring in a way that a shared key which may read *any* household
does not earn.

### ~~[5.3] Cloud SQL changes the deploy path, and the demo's cost~~ — **resolved 2026-07-16**

**Neon for the demo, Cloud SQL at pilot.** Written up as a phased approach in
`architecture.md` [4.1], which is now the home for this; the summary is that Neon runs stock
Postgres (so RLS and declarative partitioning are real, not emulated), wakes in ~300–500ms, and
costs nothing at rest — which keeps `USERS.md` §2's under-a-minute demo intact without a Cloud SQL
connector in the deploy path.

**One thing came out of writing it up that this plan does not resolve:** the phase 1 → 2 move has a
*trigger* (the first real household's data) but **not an argument**. Neon is SOC 2 Type II with
encryption at rest and in transit; nothing about real household data exceeds it. The honest case for
Cloud SQL is trust-boundary locality with KMS/Secret Manager/Cloud Run, not capability — and
"Postgres/Cloud SQL" has sat in `architecture.md` since v1 without anyone making it.

That is `prd.md` §2.4's exact shape: a decision that survived because no document ever argued for
it. It is flagged in `architecture.md` [4.1] rather than resolved here, because it is not this
plan's to spend. **Phase 1 is the only phase this plan builds.**

### [5.4] APR fallback

`architecture.md` §7.5 lists it among the decisions required before the first migration:
user-entered, estimated-with-lower-confidence, or refuse to rank (today's behavior). Archetype D
makes the consequence visible on a dashboard, which is a good way to decide it — but seeding it does
not decide it, and the schema needs to know whether an APR can have a user-entered provenance.

---

## [6] Risks

| Risk | Mitigation |
|---|---|
| **The walk refactor breaks the artifact.** Over half of `tests/test_precompute.py` (roughly lines 262–776) calls `build()` and expects an `Artifact`. | `build()` stays a thin wrapper over the new generator. The byte-identical test (`tests/test_precompute.py:489-494`) is the safety net and must stay green **without being regenerated** — regenerating it to match new output would delete the only evidence the refactor was behavior-preserving. |
| **Archetypes B/C/D make the engine refuse constantly**, producing dashboards that are honest but useless to look at. | That is a finding, not a bug — it is §9.3's cost made visible, and it is the reason to seed them. But it should be *measured* (run `calibrate` across the new archetypes) before anyone concludes the engine is broken or, worse, "fixes" it by loosening a gate. |
| **RLS is reassuring in a way a shared key does not earn.** See [5.2]. | Flag the dev posture in `USERS.md` and the ADR, not only in code comments. |
| **The new archetypes change the calibration population**, and `prd.md` §5.2 / §5.3's headline numbers (2.3% breach, 0 in 590, ~$544K) were measured on the old one. | Do not silently re-baseline. If the population changes, the numbers are re-measured and the change is stated. Those figures are cited in `prd.md`, `strategy.md`, and `decision-engine.md`. |
| **Scope creep into Plaid**, since the schema is shaped by data Plaid will supply. | `items`/`plaid_tokens`/`transactions`/`recurring_events`/`payments` are named in `architecture.md` §4 but **not created** here. Empty tables invite guessed columns; the migration is cheap when the shape is known. |
