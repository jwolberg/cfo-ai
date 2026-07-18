---
id: "0048"
title: Cut the API over — session-derived, membership-authorized household
type: feat
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0046", "0047"]
created: 2026-07-18
---

# Cut the API over — session-derived, membership-authorized household

Unit **U3**. Every user route requires a verified session and authorizes the household against
membership; internal machine routes keep service auth; `POST /plaid/link/exchange` derives
`household_id` from the session instead of the request body — the **direct fix to the premise that
started this rung** ("Plaid won't work without a real authed user").

Requirements: **KTD-2, KTD-8**; the premise.

## What changes

- `backend/main.py` — swap `require_api_key` for `authorize_household` on the household routes.
  `GET /households` becomes **"your households"** (membership-scoped via `current_user` +
  `households_for_user` directly — it has no path id), never listing all. **Also fix
  `POST /assistant/message`**: its `AssistantRequest.household_id` is the *other* body-supplied,
  caller-trusted id — authorize it against membership too, or it stays the one un-fixed IDOR after
  cutover. **Reads allowed for any member role; writes require `owner`** (KTD-10) — the public demo
  `viewer` reads every demo household but writes none.
- `backend/plaid/link.py` — drop `household_id` from `LinkExchangeRequest`; derive it from
  `current_user` membership. **Refuse an `is_demo` household** outright, so a real bank item can
  never attach to the demo plane.
- `backend/auth.py` — document/retire the shared key's new role (KTD-8): retired for user routes,
  not demoted to a lingering second authority. Internal routes (`/plaid/sync/worker`,
  `/plaid/sync/poll`, any Cloud Tasks target) keep OIDC/service auth untouched; the webhook doorbell
  keeps Plaid signature verification.

The deployed public demo runs on the pre-seeded read-only demo `viewer` session (KTD-10), so the
surface keeps working across cutover with no shared key and **no maintenance window**.

## Files

- `backend/main.py`, `backend/plaid/link.py`, `backend/auth.py`
- `tests/test_idor.py` (valid-but-non-member refusal on every household route)
- `tests/test_route_authz.py` (new — `GET /households` returns only the caller's; role gating)

## Acceptance criteria

- [ ] A valid session naming a **non-member** household is refused (403) on **every** household route
      (read and, later, write).
- [ ] `GET /households` never returns a non-member household.
- [ ] `POST /assistant/message` authorizes its body `household_id` against membership.
- [ ] `link_exchange` takes no `household_id` from the wire and **refuses an `is_demo` household**.
- [ ] The demo `viewer` session reads a demo household but is `403`'d on any write
      (`PATCH /policy`, `POST /attest`); it cannot see a non-demo household.
- [ ] Internal routes still authenticate by service credential, not session; webhook keeps Plaid
      signature verification.

## Notes / risks

- The route *shapes* stay (`/households/{household_id}/...`) so the deployed client and the reviewer
  `HouseholdPicker` keep working — only the *meaning* of the path segment changes.
- The valid-but-non-member refusal test is the one this whole rung exists to make pass; it is written
  here and is a hard gate again in U7.
