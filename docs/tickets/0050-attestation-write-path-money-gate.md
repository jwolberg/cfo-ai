---
id: "0050"
title: The attestation write-path (0016) — the money-gate, in shadow
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

# The attestation write-path (0016) — the money-gate, in shadow

Unit **U5**. Replace the hardcoded `attested=True` (`precompute.py:878`) with a real, card-set-scoped,
append-only attestation a user can set. Closes the named `0016` gap ("no one has ever cleared the
attestation end to end"), in shadow.

Requirements: **KTD-7**; `0016`; `engine/models.py:481` (`CoverageState`); `precompute.py:756,878`.

## What it is (KTD-7)

An append-only `card_attestations` table (`household_id`, `attested_by`, `card_fingerprint`,
`created_at`), where `card_fingerprint` is a stable hash of the attested card set.
`assemble_snapshot()` computes `attested = (a current attestation exists whose fingerprint matches
the current card set)` — so **a newly appearing card silently invalidates a stale attestation** and
drops the household back to `UNATTESTED`, which is the correct safety behavior. Attestation only moves
`UNATTESTED → COMPLETE`; it **never overrides `UNMATCHED_PAYMENT`** — a user cannot attest away a
card-shaped outflow to a card we cannot see. Route: `POST /households/{household_id}/attest`,
membership + `owner`-authorized.

## Files

- `alembic/versions/0013_card_attestations.py` (new; `HOUSEHOLD_SCOPED`, append-only)
- `backend/db/repository.py` (`add_attestation`, `current_attestation`)
- `backend/precompute.py` (`assemble_snapshot()` computes `attested` from a current
  fingerprint-matching attestation instead of `True`)
- `backend/main.py` (the `POST /attest` route)
- `tests/test_attestation.py` (new)

## Acceptance criteria

- [x] Attest → coverage `COMPLETE` and the money-gate clears (in shadow).
- [x] An unattested household is refused `CARD_COVERAGE_INCOMPLETE`.
- [x] A household with an unmatched card payment **cannot** be attested to `COMPLETE`
      (`UNMATCHED_PAYMENT` overrides).
- [x] Adding a card changes the fingerprint → re-triggers `UNATTESTED`.
- [x] A non-member `403`s; cross-household scope holds.

## Notes / caveat

`card_fingerprint` canonicalization + a removal test are logged FYIs — the hash must be stable across
card ordering and sensitive to add/remove. Same live-serving caveat as U4: `readpath.py` serves
frozen snapshots, so a live decision reflects the attestation only once its snapshot is re-assembled
(the live-assembly Prerequisite).
