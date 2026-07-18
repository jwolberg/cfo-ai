---
id: "0055"
title: Mobile — the real Stytch Expo sign-in flow, and an on-device / deployed-web run
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0051", "0052", "0054"]
created: 2026-07-18
---

# Mobile — the real Stytch Expo sign-in flow, and an on-device / deployed-web run

`0051`/`0052` built the mobile session cutover and the write screens and verified them to `tsc` +
`jest`, but two things were explicitly **not exercised** (no device, no Stytch project at the time):
the **real Stytch Expo sign-in UI** and any **on-device / deployed-web run**. This closes both — the
mobile analogue of what `0053` did for the backend.

## The seam that exists

`mobile/src/api/session.ts` already isolates the token: `signInWithToken(token)` (SecureStore on
native), `signOut()`, and a pre-seeded `EXPO_PUBLIC_DEMO_SESSION` for the public web demo (KTD-10).
The client sends `Authorization: Bearer …`. **What is missing is the flow that produces the token** —
the Stytch Expo sign-in UI that hands its `session_jwt` to `signInWithToken`.

## Scope

1. **Wire the Stytch Expo SDK sign-in** (magic link / OTP) into a minimal sign-in screen; on success,
   call `signInWithToken` and route into the app. A signed-out native user (no stored token, no demo
   session) lands here rather than on a blank screen.
2. **Confirm the token discipline on a device**: stored in SecureStore/Keychain, never `AsyncStorage`,
   never logged; `signOut` clears it.
3. **Run it for real**: sign in on a simulator/device against the Stytch **test** project, reach the
   user's own household; and verify the **deployed web demo** (`0054`) loads on the baked demo session
   with no login. A green jest suite is not evidence for this ticket — the point is the real run.
4. Provider-swap note: `0047`'s `deps.py` is vendor-agnostic, but this binds the *mobile* client to
   Stytch's Expo SDK — the second integration point the plan's Decision 1 flagged. Keep it isolated.

## Acceptance criteria

- [ ] A real sign-in against the Stytch test project yields a session the deployed API accepts; the
      user reaches their own household.
- [ ] The token is stored in SecureStore (verified on a device), never logged; `signOut` clears it.
- [ ] The public web demo loads with no login on the pre-seeded `viewer` session.
- [ ] A signed-out native user sees the sign-in screen; a signed-in user with zero memberships still
      sees `NoHousehold` (from `0051`).
- [ ] Provenance recorded (green on a real device/deploy on <date>), like `0053`.

## Notes

- Depends on `0054` (the API + demo session must be deployed for a real end-to-end run).
- The zero-membership `NoHousehold` screen and the write screens already exist and are jest-covered;
  this ticket is the live-flow + on-device verification they could not get in `0051`/`0052`.
