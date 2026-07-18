---
id: "0047"
title: The Stytch verification adapter + current_user / authorize_household
type: feat
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0046"]
created: 2026-07-18
completed: 2026-07-18
---

# The Stytch verification adapter + current_user / authorize_household

Unit **U2**. The `backend/identity/` module: local JWKS session verification, JIT user provisioning,
and the two FastAPI dependencies that turn a verified session into a membership-scoped repository —
with a real-Stytch sandbox gate that skips loudly when creds are absent.

Requirements: **KTD-2, KTD-4, KTD-5, KTD-9**.

## What it is

`backend/identity/` isolates the vendor:

- `stytch.py` — `verify(session_token) -> (stytch_user_id, claims)`. Verifies the session **locally
  against Stytch's JWKS**, no per-request round-trip, exactly as `backend/plaid/webhook.py:61-89`
  verifies Plaid's ES256 JWT: fetch+cache the signing key by `kid`, `jwt.decode(...,
  options={"require": ["iat","exp"]})`, reject bad signature/alg/expiry/replay. **Only this file
  knows the word "Stytch."**
- `deps.py` — `current_user()` verifies + JIT-provisions a `users` row on unknown `stytch_user_id`
  (idempotent on second login). `authorize_household(household_id)` calls `households_for_user`,
  raises `403` on non-membership, else yields a household-scoped repository (composes with the
  existing `repository()` seam). **Provider-agnostic — names no vendor.**
- The JWKS cache with **TTL-based eviction** (not the never-evict Plaid pattern) — a session key is
  verified repeatedly over its life, so a rotated/compromised signing key must stop being trusted
  within a bounded window, not just on process restart.

**Session lifecycle.** Local JWKS verification trusts a token on signature + expiry alone. Default
to short session TTL + refresh so logout/revocation take effect within the TTL. Confirm Stytch's
session shape (short-JWT-plus-refresh vs. long opaque requiring introspection) per the plan's
Deferred Notes; if the latter, reconsider local-only verify.

**Secret discipline (KTD-4).** The Stytch secret follows the Plaid/Method at-rest guard
(`assert_plaid_tokens_safe_at_rest` shape): env-flag guard now, never logged, sandbox/production keys
never interchangeable. Its secrets-manager trigger is the **first real (non-sandbox) signup**, not
the money-on trigger.

**Step-up seam (KTD-9).** `authorize_household` is the single choke point a future step-up factor
gates on. Left as a documented extension point, not implemented. The live-mode boot guard (refuse to
boot in `TRANSFER_MODE=live` unless step-up is wired for loosening writes) is designed/asserted here,
enforced at money-on.

## Files

- `backend/identity/__init__.py`, `stytch.py`, `deps.py` (new)
- `backend/db/session.py` (the Stytch secret guard)
- `tests/test_identity_deps.py` (new — stubbed verifier)
- `tests/test_identity_stytch_sandbox.py` (new — `skipif` without sandbox creds)

## Acceptance criteria

- [x] A tampered / expired / replayed / wrong-alg token is rejected **before any DB touch**.
- [x] A valid token JIT-provisions a `users` row exactly once (idempotent on second login).
- [x] `authorize_household` yields a scoped repo for a member and `403`s a valid **non-member**.
- [x] JWKS cache evicts on a TTL (rotated key stops being trusted within a bounded window).
- [x] The secret guard raises in a simulated production config with a plaintext credential; the
      Stytch secret never appears in logs.
- [x] The dependencies gate a throwaway route correctly under a stubbed verifier.
- [x] The sandbox gate skips **loudly** with its full reason when creds are absent.

## Notes / risks

- JIT concurrent-first-login race is a logged FYI — handle with an upsert / unique-violation retry.
- `deps.py` must remain vendor-free so a provider swap touches only `stytch.py` (+ the mobile SDK in
  U6a).
