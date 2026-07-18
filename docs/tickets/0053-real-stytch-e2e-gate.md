---
id: "0053"
title: The real-Stytch end-to-end gate (the unit that makes the rest real)
type: testing
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0046", "0047", "0048", "0049", "0050", "0051", "0052"]
created: 2026-07-18
completed: 2026-07-18
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

- [x] A real session verifies and provisions a `users` row.
- [x] A real non-member session is refused on every household route.
- [x] The policy write and attestation write are visible and audited (attributed to the real user).
- [x] Two sandbox users are mutually invisible.
- [x] The suite skips **loudly** without creds, naming exactly what is missing.
- [x] Provenance + the vendor-reality correction recorded (test docstring + implementation-notes).

## Status (2026-07-18) — GREEN

Run **green against a real Stytch test project on 2026-07-18** (4 passed): a real `session_jwt`
minted via the Passwords product verifies through our own local JWKS adapter and JIT-provisions the
`users` row; a real non-member session is `403`'d; a member's `PATCH /policy` and `POST /attest` land
and are attributed to the real user id; two real sessions are mutually invisible.

**One vendor-reality correction** (the plan budgeted for ≥1): the JWKS host is environment-specific —
Stytch serves Test from `test.stytch.com`, not the `api.stytch.com` an earlier draft derived
unconditionally (which 404s a test project). Fixed in `backend/identity/stytch.py` (`_stytch_host`),
the one file that names the vendor. The session-JWT shape the adapter verifies
(`iss = stytch.com/<pid>`, `aud = [<pid>]`, `sub` = the Stytch user id) was confirmed **correct** — no
change needed there.

Without creds the gate still **skips loudly** (verified: 4 skipped with the full reason), so CI keeps
proving it *can* run, not that it *did*. `stytch` (the server SDK, used only to mint sessions) is now
a `[dev]` extra. The `_mint_session` `NotImplementedError` placeholder is gone.
