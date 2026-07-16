---
id: 0004
title: Postgres scoped by household, replacing the generated JSON artifact
anchor: ADR-0004
status: accepted
date: 2026-07-16
supersedes: 0002
superseded-by:
---

## [1] Context

[ADR-0002](./0002-generated-json-artifact-over-database.md) chose a single generated JSON file over
a database, and named the four conditions that made it correct: **one** household, **90 days** of
records, **read-only at runtime**, and **fixed** content. It then said what would end it:

> A second household, a user-editable policy, or a live daily decision all require a real database,
> and this decision would be **superseded rather than extended** — the artifact is regenerated
> wholesale, has no write path, and holds everything in memory.

It called its own conditions exactly right, and they have now been met. The demo needs households
that are not the demo household — see
[`../brainstorms/2026-07-16-multi-tenant-persistence-and-seeded-households.md`](../brainstorms/2026-07-16-multi-tenant-persistence-and-seeded-households.md)
[0.2], which found that **every household this engine has ever run against is biweekly with one
card at 23.99% APR**, including all 60 in the calibration population. The axes that distinguish
households exist in `sim/` and are unused, and
[`../decision-engine.md`](../decision-engine.md) §9.3 already documents a rule that is wrong for
households the engine has never met.

This ADR is not a correction of ADR-0002. It is the event ADR-0002 predicted.

## [2] Decision

**Postgres, scoped by `household_id`, on Neon through the demo.** Schema, phases, and the
household-vs-user correction live in [`../architecture.md`](../architecture.md) [4] and [4.1] rather
than here, because that file is evergreen and this one is a point in time.

**The argument is reversibility, not scale.** This matters enough to state plainly, because the
question that prompted this work was "how do we scale to millions of users" and **that is not the
reason, and could not be.**

Two facts kill the scale argument in both directions:

- [`../prd.md`](../prd.md) §2.2 says, verbatim: *"Segment sizing should assume roughly 15–20% of
  consumers, not 'millions' unqualified."* The engine's own variance gate is what makes that true.
- The arithmetic says Postgres was never in danger. One decision per household per day is ~**58
  writes/sec at 5M households** ([`../architecture.md`](../architecture.md) [4.1]), and a measured
  `Snapshot` is **2,699 bytes** compressing ~70× when sorted by `(household_id, day)` — **under a
  TB/yr at 5M**. An earlier draft of [4.1] reasoned about a "phase 3" from a *guessed* 10KB
  snapshot and an 18 TB/yr scan. Both were wrong. The guess is corrected in place, and the
  correction is why this section does not lean on a number.

So the decision rests on **which choices are expensive to reverse**, an argument that is true at 60
households and at 5M and therefore never has to cite a user count:

| Built now | Why now | Cost of retrofitting |
| --- | --- | --- |
| **`household_id` scoping + RLS** | Every table, every query, every policy | Re-scoping a live schema; an IDOR in the interim exposes a complete financial life ([4]) |
| **Monthly partitioning** on `decisions`/`transactions` | Free at zero rows | A migration scheduled around a 365M-row table |
| **`snapshot_ref` behind a two-method `SnapshotStore`** | Postgres TOAST compresses each value independently (~3.5×); a sorted columnar store gets ~70× | Extracting a multi-TB column out of a live partitioned table |
| **`dek_id` on `households`** | Resolves [`../architecture.md`](../architecture.md) §7.3's CCPA-vs-append-only tension in the schema, as it asks | Encrypting data that is already written, under a key that does not exist |

| Deferred | Why | Cost of retrofitting |
| --- | --- | --- |
| **Real KMS wiring** | Synthetic households have no PII. The *column* is the expensive half | A config change and a backfill |
| **Object storage for snapshots** | The seam makes it a swap | None — that is what the seam is for |
| **Read replicas, AlloyDB** | Wire-compatible dials | None |
| **Plaid tables** (`items`, `transactions`, `recurring_events`, `payments`) | Named in [4], shape not yet known. Empty tables invite guessed columns | A migration, once the shape is real |

**Decide now, wire when something real needs it.** That is
[`../architecture.md`](../architecture.md) [1.1]'s fifth principle — *build the smallest thing that
can be wrong in public* — not a deferral of the decision.

## [3] Consequences

A database to provision, migrate, and back up; a connection in the deploy path; migrations in CI.
That is the cost, and ADR-0002 bought three years of nothing by avoiding it for three days.

Neon rather than Cloud SQL through the demo keeps most of it cheap: stock Postgres (so RLS and
declarative partitioning are real, not emulated), ~300–500ms cold start, nothing at rest. The demo
that [`USERS.md`](../../USERS.md) §2 says must work in under a minute still does. **The move to
Cloud SQL has a trigger but not yet an argument** — flagged in
[`../architecture.md`](../architecture.md) [4.1], and deliberately not spent here.

**ADR-0002's three safety properties do not survive by inheritance. Two get stronger, one has to be
rebuilt:**

1. **Exact `Decimal` round-trips — stronger, and the `$dec` hack shrinks.** Postgres `NUMERIC` is
   exact natively, so `decisions.amount` and every money column need no tagging at all. The
   tagged-scalar codec (`backend/artifact.py`'s `_encode`/`_decode`) survives for exactly one job:
   the frozen snapshot payload, which is JSON and therefore still has no decimal type. `money()`
   remains the only sanctioned constructor and still refuses floats.
2. **Fail fast, never per-request — rebuilt, not inherited.** A database cannot validate every row
   at startup the way a 90-day file could. The property is preserved where it is cheap and real:
   the **seeder** validates on write, and reads validate on the way out. A service holding bad data
   must still refuse rather than serve wrong numbers one request at a time.
3. **A stale artifact is a test failure — carried, and re-pointed.** The seeder is deterministic
   from `(spec, seed)`, so the equivalent test is that re-seeding produces identical rows. Until
   that exists, `tests/test_precompute.py`'s byte-identical check is the regression oracle for the
   demo household and **must stay green without being regenerated**.

**The artifact stops being persistence and becomes a fixture.** `backend/data/decisions.json` is no
longer served; it is the golden file proving archetype A's decisions did not move. This is not
ADR-0002 being "extended" — nothing reads it at runtime, there is one system of record, and the file
is a test input like any other.

## [4] Conflicts resolved

- **C1 — Postgres vs. a horizontally-partitioned store** (Cassandra/DynamoDB/Spanner): chose
  Postgres. At ~58 writes/sec it would buy throughput we do not need and cost three things we
  depend on: RLS ([`../architecture.md`](../architecture.md) [4]), cross-entity transactions, and
  decimal exactness — the subject of ADR-0002's own [2.2].
- **C2 — Neon vs. Cloud SQL for the demo:** chose Neon, for cold-start and cost, and because stock
  Postgres means the RLS this demo is meant to prove is the real thing. Cloud SQL is phase 2.
- **C3 — snapshot payload inline vs. behind a seam:** chose the seam, on the measured 3.5×-vs-70×
  compression gap, not on volume. A reviewer will ask whether this repeats
  [`../architecture.md`](../architecture.md) [1.2]'s `FinancialProvider` mistake ("you cannot design
  the seam from n=1"). It does not: `put(bytes) → ref` / `get(ref) → bytes` is one known shape with
  one known consumer, not an abstraction over vendors we have never called.
- **C4 — CCPA deletion vs. the append-only decision log:** chose **crypto-shredding**. One DEK per
  household; erasure destroys the key. Rows remain, bytes become unrecoverable, the audit trail
  holds, and decision *counts* still aggregate — so erasure does not punch holes in the population
  [`../prd.md`](../prd.md) §5.2 says must be measured population-wide.
- **C5 — tenant is the household, not the user:** chose `household_id`. Corrected in
  [`../architecture.md`](../architecture.md) [4], which carried `user_id` until today. A `user` is a
  login; one household may have two, and for this product that is not a footnote — a spouse's
  spending is precisely what breaks a forecast.
- **C6 — build the scale apparatus now vs. the reversible decisions now:** chose the latter. See
  [2]'s tables.

## [5] Unchanged and still binding

- **The engine is untouched.** `engine/` and `sim/` keep `dependencies = []`. `pyproject.toml`'s
  comment stands: *"engine/sim stay dependency-free — this is the web shell around them."*
- **`money()` is still the only sanctioned way to construct a dollar amount**, and it still refuses
  floats (`engine/models.py`).
- **Refusal is still the default** and every gate still fails closed. Nothing here touches
  `decide()`.
- **[`../architecture.md`](../architecture.md) [3.3]'s frozen snapshot is not weakened.** Store the
  inputs, not references to the inputs. The seam changes *where the bytes live*, never *whether they
  are kept*.
- **[`../prd.md`](../prd.md) §2.2's variance gate is not a growth lever.** It is the one dial
  [`../strategy.md`](../strategy.md) §4 says destroys the product, and reaching a bigger user number
  is not a reason to touch it.
