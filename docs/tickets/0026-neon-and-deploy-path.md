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

- [x] Neon project provisioned; connection string in Secret Manager; nothing secret in the deploy
      command or the repo. — *Neon provisioned and `cfo_runtime` created (`0020`);
      [`neon-provisioning.md`](../runbooks/neon-provisioning.md) carries it. **Secret Manager done
      2026-07-16** (`deploy.md` `[4]`): the connection string is a secret reference, version 2 —
      version 1 held the pre-correction string and is stale. The bind was verified as `cfo_runtime`
      **through the pooler, using the Secret Manager value itself** rather than a hand-typed one:
      an unscoped read of `decisions` returned 0 rows, a scoped read returned only the household's.*
- [x] Migrations run in the deploy path; the step is documented. — **Decided: by hand, before the
      deploy, not in the entrypoint** (`deploy.md` `[2]`). An entrypoint that migrates has every
      cold start racing for a schema lock on a service that scales, and `--max-instances=1` is a
      policy rather than a guarantee. The manual step is safe *because* of the AC below: a
      forgotten migration is a failed deploy, not a service answering 500s. Documented in
      `docs/runbooks/deploy.md` rather than `DEPLOY.local.md` — the latter is gitignored, and a
      procedure nobody but its author can read is not documentation. That file's own §12 asked for
      this graduation, and it was `0009`'s last piece.
- [x] A service started against an unmigrated or unreachable database **fails fast** rather than
      serving. — *`main.py`'s lifespan: `assert_rls_binds()` + `_assert_migrated()`. Both are
      startup-fatal and both are tested (`TestStartup`), including the one a ping would miss — a
      role that reaches the database and bypasses RLS.*
- [ ] **Cold-start measured, not assumed:** scale to zero, hit `/households/{id}/decisions`, record
      the wall-clock in the PR. Compare it against `REQUEST_TIMEOUT_MS` (8s), not against the ~500ms
      brochure figure. — *`deploy.md` `[6a]` is the measurement. Needs a real deploy.*
- [ ] `docs/RUNBOOK.md` covers: rotating the connection string, what a cold start looks like, and
      what to do when the database is unreachable. — *`RUNBOOK.md` now covers local Postgres and
      seeding (`0031`). Rotation lives in `neon-provisioning.md`; the unreachable-database symptom
      table is `deploy.md` `[5]`. What is missing is the cold-start section, which cannot be written
      before it is measured.*
      *(2026-07-21: the precondition — "needs a real deploy" — is now met. The 0057 stage-3 deploy
      superseded `resfi-api-00003-viv`, and prod answers authed reads. Both remaining ACs are
      unblocked; what is left is doing the measurement, then writing the cold-start section.)*
- [x] The deployed demo serves the four archetypes. — **Done 2026-07-16.** This was called "the
      blocker, and it is not code", and that was right: the fix was running `deploy.md` `[3]`.
      Revision `resfi-api-00003-viv` serves `demo_biweekly`, `semimonthly_portfolio`,
      `monthly_thin`, and `apr_unreported` — 90 days each — out of Neon at `0004 (head)`, over
      `/households/{id}/…`. `/health` answered `{"status":"ok"}` for the first time in the repo's
      history, and an unknown household `404`s.

## Prepped by `0031` (2026-07-16) — everything short of touching live infrastructure

`0031` made the database non-optional for every route, so it inherited this ticket's blast radius.
What it did, and deliberately did not do:

- **The manifest is not this branch's fix.** PR #50 found and fixed it — `requirements.txt` had no
  database driver while `main.py` has imported SQLAlchemy since `0024` — and added
  `tests/test_requirements.py`, which walks the imports from the AST. This branch takes both
  unchanged. (An earlier draft of this branch fixed it independently and added `alembic` to the
  manifest, justified by "the deploy migrates from the image". That was wrong: `[2]` below migrates
  from a developer's machine. #50's list is correct.)
- **[`docs/runbooks/deploy.md`](../runbooks/deploy.md) is written** — `0009`'s graduation plus this
  ticket's database steps, placeholders only, no secrets.
- **Not done, by choice:** no gcloud auth used, no secret created, no Neon write, no deploy. The
  runbook's `last-verified` said `never` and steps `[2]`–`[5]` were written from the code rather
  than from a run. **Do not trust them until one of them has failed and been fixed.**

  *Superseded 2026-07-16: they ran, and the distrust was earned.* `[2]`–`[5]` went through end to
  end and the run **disproved four of the runbook's own claims** — see `neon-provisioning.md` and
  PR #54. Written-from-the-code was not good enough, exactly as this bullet warned. The runbook now
  says `last-verified: 2026-07-16`.

⚠️ **Neon predated migration `0004`.** It was migrated at `0020`; `0031` added `spend_projections`
(five scoped tables → six). The deployed code would refuse to start against it until `deploy.md`
`[2]` ran. That refusal is `_assert_migrated()` working, not a bug. **Resolved 2026-07-16:** `[2]`
ran, Neon is at `0004 (head)`, and the service starts — which is that gate passing rather than
being skipped.

## If the cold start misses

That is a **phase-2 trigger arriving early**, and it should be written up as one in
`architecture.md` [4.1] — it would be the first real argument for Cloud SQL anyone has made. It is
**not** a reason to keep the artifact as a serving path; ADR-0002 is superseded and two persistence
paths is the thing that decision explicitly ruled out.
