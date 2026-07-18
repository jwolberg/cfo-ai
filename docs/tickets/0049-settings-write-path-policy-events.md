---
id: "0049"
title: The settings write path — append-only policy_events, current state derived
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0046", "0047", "0048"]
created: 2026-07-18
completed: 2026-07-18
---

# The settings write path — append-only policy_events, current state derived

Unit **U4**. `PATCH /households/{household_id}/policy` writes a validated, audited, append-only
policy change; `Repository.policy()` reads the latest event; the seeder writes an initial event. The
product's **first real write path**.

Requirements: **KTD-6**; `engine/models.py:591` (the `UserPolicy` invariants).

## Why append-only (KTD-6)

The codebase's identity is append-only auditability — `decisions` freeze their input, `transfers`
are append-only, state is a projection. Policy is the guardrail set (safety-critical) and the admin
story already calls for "every action logged to the same append-only record as a decision." So the
source of record becomes an append-only `policy_events` table and `Repository.policy()` reads the
**latest event per household**, ordered by `seq` (a monotonic identity — not `created_at`, since a
seed transaction can tie on `created_at`; the sweep rung's `transfers.seq` lesson). The mutable
`policies` upsert is retired. History cannot be derived from current state, but current state *can*
be derived from history.

## Guardrail-loosening audit flag (KTD-9)

A policy change that **loosens** a guardrail (lowers `buffer_floor`, raises a sweep cap) is flagged
distinctly in `policy_events` so the eventual step-up retrofit and any review can find them. Ships
ungated this rung because shadow mode nulls its financial impact; the flag + the live-mode boot guard
are the two compensating controls.

## Files

- `alembic/versions/0012_policy_events.py` (new; add `policy_events` to `HOUSEHOLD_SCOPED`,
  append-only grant SELECT+INSERT; **a data-migration step backfills one `policy_events` row per
  existing `policies` row before the mutable upsert is retired** — otherwise every already-seeded
  household, including the deployed reviewer surface, reads zero events and loses its policy after
  cutover)
- `backend/db/repository.py` (`policy()` reads latest by `seq`; `set_policy` becomes an append;
  retire the mutable upsert)
- `backend/main.py` (the `PATCH` route + validation)
- `backend/seed.py` (write an initial event)
- `backend/precompute.py` (unchanged read contract — still gets a `UserPolicy`)
- `tests/test_policy_write.py` (new); update any test asserting the old mutable `policies` shape

## Acceptance criteria

- [x] Write→read roundtrip returns the new guardrails; `Repository.policy()` returns them on the
      next read, ordered by `seq`.
- [x] Validation against `UserPolicy.__post_init__` + explicit bounds (`buffer_floor ≥ 0`, caps
      `≥ MIN_SWEEP` and internally consistent, `min_days_between_sweeps` in a sane range); an
      invariant violation is rejected with **no row appended**.
- [x] A non-member `403`s.
- [x] History is non-destructive — prior values remain in `policy_events`.
- [x] Cross-household scope holds.
- [x] A loosening change is flagged distinctly in the event row.
- [x] The `policy_events` backfill runs in the migration; no already-seeded household loses its
      policy after cutover.

## Notes / caveat

No code path today reads `Repository.policy()` back into `decide()` for a live household (the demo
`assemble_snapshot()` uses a hardcoded `UserPolicy`; `readpath.py` serves frozen snapshots). So this
proves write + audit + latest-read, **not** a re-decided sweep. The live-assembly path is a named,
unresolved Prerequisite in the plan — out of scope here.
