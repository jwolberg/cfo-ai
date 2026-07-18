---
id: "0051"
title: Mobile — session auth cutover (drop the baked key)
type: feat
status: open
priority: high
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0048"]
created: 2026-07-18
---

# Mobile — session auth cutover (drop the baked key)

Unit **U6a**. Replace the baked shared key + `DEMO_HOUSEHOLD` with a Stytch session and the user's
membership-scoped households — the highest-value, lowest-risk half of the mobile work, split out so
it ships as soon as U3 lands rather than waiting on the two write screens.

Requirements: **KTD-2, KTD-4**; the premise (no shared key in the client bundle).

## What changes

- `mobile/src/api/client.ts` — send the session token; drop the static key + `DEMO_HOUSEHOLD`
  default.
- A minimal **Stytch Expo sign-in**.
- `mobile/src/components/HouseholdPicker.tsx` — becomes "your households" from membership.
  Single-membership users skip the picker; multi-membership (the reviewer) still sees it.
- `mobile/src/screens/NoHousehold.tsx` (new) — a freshly JIT-provisioned user with **zero
  memberships** lands here ("You don't have access to a household yet"), *not* a blank screen and
  *not* a signup flow (onboarding is deferred).

**Token storage.** The session token is stored in **Expo SecureStore / Keychain — never
`AsyncStorage`/plain storage — and never logged** (a higher-value bearer credential than the shared
key it replaces; mirror KTD-4).

**The demo plane (KTD-10).** The public demo build carries the pre-seeded read-only demo `viewer`
session — the visitor reaches `cfo-ai-1.web.app` with no login, exactly as today, while a real user
signs in to get their own `owner` session; the two planes never cross.

## Files

- `mobile/src/api/client.ts`, `mobile/src/components/HouseholdPicker.tsx`
- `mobile/src/screens/NoHousehold.tsx` (new); a minimal Stytch Expo sign-in
- tests under `mobile/`

## Acceptance criteria

- [ ] A signed-in member sees only their household(s); single-membership skips the picker.
- [ ] A signed-in user with **zero** memberships sees the `NoHousehold` screen, not a crash or blank.
- [ ] **No static API key remains in the bundle.**
- [ ] The session token is read from SecureStore, never logged.
- [ ] The reviewer dev user still sees all demo households; the deployed web build works for a
      signed-in member with no baked key.
