---
id: "0026"
title: Neon, and the deploy path
type: chore
status: in-progress
priority: medium
repo: cfo-ai
agentId: infra-devops-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0020"]
created: 2026-07-16
---

# Neon, and the deploy path

Implements **U8** of the plan. Single owner: `infra-devops-agent`.

**Depends on:** `0020` (schema and migrations to run).

**Files:** `DEPLOY.local.md`, `.github/workflows/`, `docs/RUNBOOK.md`, `Procfile` (only if needed)

## What changes

Today's deploy is `gcloud run deploy --source .` against a committed JSON file with **zero infra** —
that is ADR-0002's C2, and it is why the artifact was committed in the first place. This adds a
database to that path.

- A Neon project; connection string in **Secret Manager**, never a plaintext env var in the deploy
  command.
- Migrations run in the deploy path. Decide and **document** where: a release step, or an entrypoint
  guard. A service that starts against an unmigrated database must fail fast (`0024`), not serve.
- `DEPLOY.local.md` and `docs/RUNBOOK.md` updated — the runbook is live documentation, unlike the
  tickets.

## Why Neon and not Cloud SQL

[`architecture.md`](../architecture.md) [4.1] is the home for this. Summary: Neon runs **stock
Postgres**, so the RLS and declarative partitioning this demo exists to prove are real rather than
emulated; it wakes in ~300–500ms; it costs nothing at rest. Cloud SQL is phase 2.

**Do not "upgrade" to Cloud SQL as part of this ticket.** [4.1] records that the phase 1 → 2 move
has a *trigger* (the first real household's data) but **not yet an argument** — Neon is SOC 2 Type II
with encryption at rest and in transit, and the honest case for Cloud SQL is trust-boundary locality
with KMS/Secret Manager/Cloud Run, not capability. That argument has never been made, and
[`prd.md`](../prd.md) §2.4 is a whole section about a decision that survived because nobody ever made
one.

## CI stays on a container

`0020` adds a `postgres:16` service container. **CI must not point at Neon** — secrets, cost, and
flakiness. Neon is stock Postgres, so the drift is nil. This ticket does not change that.

## ⚠️ Measure the cold start. Do not trust the brochure.

[`USERS.md`](../../USERS.md) §2: the reviewer wants to see this work **in under a minute**. That
constraint is why Neon was chosen over Cloud SQL, so it is the one thing here worth measuring rather
than assuming.

Note the interaction with `0009`'s `--min-instances=1`, which already bills ~$10–15/mo to keep Cloud
Run warm *precisely because* the client gives up after 8s (`REQUEST_TIMEOUT_MS`). **A warm container
in front of a cold database is a new version of the same problem** — and this time the timeout budget
is already spent.

## Acceptance criteria

- [ ] Neon project provisioned; connection string in Secret Manager; nothing secret in the deploy
      command or the repo.
- [ ] Migrations run in the deploy path; the step is documented in `DEPLOY.local.md`.
- [ ] A service started against an unmigrated or unreachable database **fails fast** rather than
      serving.
- [ ] **Cold-start measured, not assumed:** scale to zero, hit `/households/{id}/decisions`, record
      the wall-clock in the PR. Compare it against `REQUEST_TIMEOUT_MS` (8s), not against the ~500ms
      brochure figure.
- [ ] `docs/RUNBOOK.md` covers: rotating the connection string, what a cold start looks like, and
      what to do when the database is unreachable.
- [ ] The deployed demo serves the four archetypes.

## If the cold start misses

That is a **phase-2 trigger arriving early**, and it should be written up as one in
`architecture.md` [4.1] — it would be the first real argument for Cloud SQL anyone has made. It is
**not** a reason to keep the artifact as a serving path; ADR-0002 is superseded and two persistence
paths is the thing that decision explicitly ruled out.
