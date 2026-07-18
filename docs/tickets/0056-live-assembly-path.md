---
id: "0056"
title: The live-assembly path — a settings/attestation write that changes a live decision
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0049", "0050"]
created: 2026-07-18
---

# The live-assembly path — a settings/attestation write that changes a live decision

The named, unresolved Prerequisite of the identity rung (and, before it, the sweep rung's own plan —
both flagged the identical gap). `0049` and `0050` proved the settings and attestation writes are
**persisted, audited, and read back** — but **not that they change a live decision**, because nothing
reads `Repository.policy()` / `attested_for` into `decide()` for a live household.

## Why the writes don't yet bite

Two independent reasons, both in the plan's Prerequisites:

- The demo `assemble_snapshot()` takes a **hardcoded `UserPolicy`** (`backend/precompute.py`), and the
  `attested` bool is a parameter defaulting `True` (`0050`) — the walk/replay/seeder pass the defaults
  on purpose (replay must grade the shipped engine), so a seeded household never reflects a written
  policy or attestation.
- `backend/readpath.py` serves **frozen precomputed snapshots**, so even a re-written policy is not
  re-assembled into the served decision.

So a user can lower their buffer floor or attest their cards, and `Repository.policy()` /
`attested_for` will return the new values — but the decision the app shows was computed from the old
ones.

## Scope

Build a **live per-household assembly path** that, for a real (non-frozen) request, reads the current
policy (`Repository.policy()` → `UserPolicy`) and the current attestation
(`backend/attestation.py:attested_for`) and threads both into `assemble_snapshot(..., attested=…)` /
`decide()`, so a write changes the next decision. Keep `assemble_snapshot` **pure** — the live values
are *passed in*, exactly as `sweeps_in_flight` and `attested` already are, so `backend/replay.py`
keeps grading the engine that shipped.

This likely pairs with the deferred **recurring-event detector** and the real Plaid data path (a live
household is a *linked* one), so scope it against the next rung rather than the frozen demo — or build
the seam now and let a linked household be its first consumer.

## Acceptance criteria

- [ ] A live request assembles the snapshot from the household's **current** `policy_events` and
      `card_attestations`, not a hardcoded policy or `attested=True`.
- [ ] Lowering `buffer_floor` (or attesting) via the write path changes the **next** decision for that
      household — proven end to end, not just in the stored event.
- [ ] `assemble_snapshot` stays pure over its inputs; `backend/replay.py`'s regression oracle is
      unaffected (the walk/replay still pass the defaults).
- [ ] The `UNMATCHED_PAYMENT` override still holds — a live attestation cannot clear it.

## Notes

- This is the honest close of the `[~]` caveats in `0049`/`0050` and the shadow caveats in their code.
- The sweep rung's plan (`docs/plans/2026-07-17-002-…`) flagged this same gap unresolved; closing it
  here also unblocks a live-decided **sweep** reflecting a written guardrail.
