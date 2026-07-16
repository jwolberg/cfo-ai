---
id: 0002
title: A generated JSON artifact, not a database, for the demo's decision history
anchor: ADR-0002
status: superseded
date: 2026-07-13
supersedes:
superseded-by: 0004
---

> **Superseded 2026-07-16 by [ADR-0004](./0004-postgres-scoped-by-household.md)** — on the exact
> condition [3] named ("a second household"), by the mechanism it named ("superseded rather than
> extended"). This document was right for three days of product decisions and is kept for the
> reasoning, not the outcome. Its three enforced properties did not survive by inheritance; see
> ADR-0004 [3] for which got stronger and which had to be rebuilt.

## [1] Context

The frontend MVP (`docs/plans/2026-07-13-001-feat-decision-engine-frontend-mvp-plan.md`)
needs to serve one demo household's decision history to a dashboard and an explain
assistant. `docs/architecture.md` names Postgres as the eventual home for decisions, and
the origin requirements doc left the persistence layer as an open question.

The data this MVP actually serves is:

- **One** household, pre-authenticated, with no sign-up and no second user.
- **90 days** of decisions — a few hundred records, not a few hundred thousand.
- **Read-only at runtime.** Decisions are precomputed at build time; nothing in the running
  service writes. There is no "run today's decision" action in scope.
- **Fixed.** The window is pinned to calendar dates, so two deploys a week apart serve
  byte-identical content.

## [2] Decision

Persistence is a **single generated JSON file**, `backend/data/decisions.json`, produced by
`backend/precompute.py` and **committed to the repo**. `backend/main.py` loads it once at
startup, validates it, and serves it from memory.

The artifact is committed rather than generated during deploy: `gcloud run deploy --source .`
packages whatever is in the source tree, so a committed file needs no extra build wiring in
the deploy path.

Three properties make this safe rather than merely convenient, and they are enforced in code
(`backend/artifact.py`), not by convention:

1. **Fail fast at startup, never per-request.** A missing, malformed, or internally
   inconsistent artifact raises `ArtifactError` and the service refuses to come up. A
   service holding bad data should not serve 500s — or worse, wrong numbers — one request
   at a time.
2. **Exact `Decimal` round-trips.** `Reason.params` is an open mapping whose values are
   `Decimal` amounts, `date`s, and enums. Plain JSON would flatten each to a string or a
   **float**, and a cent that round-trips through a float is no longer the cent the engine
   decided on. So every scalar is tagged with its type (`{"$dec": "400.00"}`) and
   reconstructed exactly. Floats never touch a dollar amount in either direction.
3. **A stale artifact is a test failure.** Because the file is committed, it can drift from
   the code that generates it. `tests/test_precompute.py` regenerates it and asserts the
   bytes match, so CI catches the drift rather than a reviewer noticing a strange number.

## [3] Consequences

No database to provision, migrate, back up, or pay for; no ORM; no connection pooling; no
schema migration story for a service that never writes. Startup is a file read. The whole
persistence layer is auditable by opening one file in an editor.

The cost is that this **does not generalize**. A second household, a user-editable policy, or
a live daily decision all require a real database, and this decision would be superseded
rather than extended — the artifact is regenerated wholesale, has no write path, and holds
everything in memory. That is the correct trade for a single-tenant demo of read-only
synthetic data, and Postgres remains the documented target (`docs/architecture.md`) the
moment the product outgrows those conditions.

The committed artifact also means a change to the demo spec, seed, or policy is a change to
a **checked-in file** and shows up in the diff — which is a feature: the demo's data is
reviewable, and it cannot change silently between deploys.

## [4] Conflicts resolved

- **C1 — Postgres/Cloud SQL vs a generated file:** chose the file. A few hundred read-only
  records for one household does not need a database, and provisioning one buys nothing this
  MVP can use.
- **C2 — generate the artifact at deploy time vs commit it:** chose to commit it. Buildpacks
  package the source tree as-is, so committing needs no deploy-time wiring, and the staleness
  risk it introduces is closed by a test rather than left open.
- **C3 — plain JSON vs tagged scalars:** chose tagged. Plain JSON silently turns `Decimal`
  into `float`, which is the single failure `engine/models.py`'s `money()` exists to prevent.

## [5] Unchanged and still binding

- `money()` remains the only sanctioned way to construct a dollar amount, and it still
  refuses floats (`engine/models.py`).
- The engine is untouched. `backend/precompute.py` is a new *consumer* of `engine/` and
  `sim/`; no decision logic moved, and nothing in `engine/` knows this artifact exists.
- `docs/architecture.md`'s eventual Postgres design is not withdrawn, only deferred.
