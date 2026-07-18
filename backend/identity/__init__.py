"""The identity edge — a verified user, and the household authorization that follows from it.

Ticket 0047 (the identity rung's U2). Two modules, one boundary:

- `stytch.py` verifies a session token **locally against Stytch's JWKS** and returns the vendor's
  user id — the only file in the codebase that knows the word "Stytch".
- `deps.py` turns that into `current_user` (verify + JIT-provision) and `authorize_household`
  (membership check → scoped repository). It names no vendor, so swapping providers later swaps
  `stytch.py` alone (plus the mobile sign-in SDK in U6a).

The verification mirrors `backend/plaid/webhook.py`: a JWT, verified against a fetched-and-cached
signing key, rejected on bad signature/alg/expiry — no per-request round-trip to the vendor.
"""

from __future__ import annotations
