---
id: "0053"
title: The real-Stytch end-to-end gate (the unit that makes the rest real)
type: testing
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0046", "0047", "0048", "0049", "0050", "0051", "0052"]
created: 2026-07-18
---

# The real-Stytch end-to-end gate (the unit that makes the rest real)

Unit **U7**. Drive the whole path against a **real Stytch sandbox** — a green *mocked* suite is
explicitly **not** accepted as evidence. This repo's recurring defect class is "a mechanism built,
tested, and never actually exercised" (0021, 0033, 0038; the sweep rung's U6); this unit is the
antidote for the identity rung.

Requirements: the "built, tested, never exercised" discipline; the sweep rung's U6 as template.

## Approach

Use **real Stytch sandbox sessions**, not a stubbed verifier: mint a session for a sandbox user, and
drive:

1. `current_user` JIT provisioning (a real session provisions a `users` row).
2. `authorize_household` membership + `403` for a real non-member session on every household route.
3. A policy `PATCH` — assert the audit trail in `policy_events`.
4. An `attest` — assert coverage → `COMPLETE` (money-gate clears in shadow).
5. A second sandbox user proving mutual isolation.

Document provenance ("green against real Stytch sandbox on <date>") and any vendor-reality correction
(budget for ≥1, as the Plaid and sweep rungs each hit). `skipif` **loudly** without Stytch sandbox
creds, mirroring `tests/test_plaid_sandbox.py`.

## Files

- `tests/test_identity_sandbox.py` (new — the hard gate; `skipif` without Stytch sandbox creds)
- confirm `tests/test_idor.py`'s membership leak test is non-vacuous

## Acceptance criteria

- [ ] A real session verifies and provisions a `users` row.
- [ ] A real non-member session is refused on every household route.
- [ ] The policy write and attestation write are visible and audited.
- [ ] Two sandbox users are mutually invisible.
- [ ] The suite skips **loudly** without creds, naming exactly what is missing.
- [ ] Provenance + any vendor-reality correction recorded (in the test docstring and
      `docs/implementation-notes.md`).
