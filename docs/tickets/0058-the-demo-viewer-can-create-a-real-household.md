---
id: "0058"
title: The public demo viewer can create a real household — POST /households has no plane gate
type: fix
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
depends_on: []
created: 2026-07-21
---

# The public demo viewer can create a real household — `POST /households` has no plane gate

Found by measuring the deployed API on 2026-07-21 (the fail-closed check owed by
`docs/plans/2026-07-21-001-demo-read-only-public-demo-plan.md`). Every other owner-gated write is
refused to the public demo session. **This one is not.**

Measured against `https://resfi-api-ax7jrjo2tq-uc.a.run.app` with a token from the unauthenticated
`POST /demo/session`:

| Call | Result |
|---|---|
| `PATCH /households/{h}/policy` | 403 — "This action requires an owner of the household." |
| `POST /households/{h}/attest` | 403 — same |
| `GET /households/{h}/policy` for a household the viewer is not in | 403 |
| no token / tampered token | 401 |
| **`POST /households`** | **201 — created** |

## Why it happens

`POST /households` (`backend/main.py:470`) depends on `current_user` alone. It correctly does **not**
call `authorize_household` — there is no household yet to authorize against — but that means there is
no role gate on the route at all, and role is the only thing that makes the demo viewer read-only.
`create_household(engine, owner_user_id=user.id)` (`backend/db/repository.py:579`) then makes the
caller **owner** of the new, `is_demo = false` household.

## Why it matters

It is an escalation, not just a stray row. Once the demo viewer owns a non-demo household, the writes
it was just refused become available *on that one*: `PATCH /policy`, `POST /attest`, and
`POST /plaid/link/exchange` — `_linkable_household` resolves to "the one non-demo household you own."

This falsifies two claims currently in writing:

- `backend/identity/demo.py` — "a public, unauthenticated `POST /demo/session` handing this token to
  anyone is safe by construction: the worst a caller can do is read demo-plane households."
- `docs/status.html:649` — "the demo viewer structurally read-only."

KTD-10 ("the two planes cannot cross") holds for reads and breaks here: the demo identity can reach
the real plane by creating it.

## What limits the blast radius

- The route is **idempotent per user** and the demo viewer is a single shared identity, so this is one
  household total, not unbounded creation.
- Prod is `PLAID_ENV=sandbox` and `PLAID_ENV=production` is structurally blocked at boot
  (`assert_plaid_tokens_safe_at_rest()`), so link-exchange cannot reach real accounts.
- No other household's data is exposed — the created household is the caller's own.

Neither limit is the *stated* safety property, which is that the demo credential cannot write.

## Fix

Gate the route on **identity**, not role — role is per-household and there is no household yet. The
demo viewer is a known, configured user (`DEMO_STYTCH_EMAIL`), and the same reconciliation the
importer does (`scripts/seed_demo_household.py`) already ties it to a specific `users` row. A
demo-plane identity must be refused `POST /households` structurally, so a future demo identity that
someone forgets to special-case fails closed.

Prefer a property of the *user row* (e.g. a demo flag reconciled at import) over matching on the
configured email at request time — an env-var comparison is deploy-discipline again, which is the
thing 0057 chose its lane to avoid.

## Acceptance criteria

- [ ] `POST /households` with a demo-plane session is refused (403), verified against the **deployed**
      API, not only locally.
- [ ] The refusal is structural — a new demo identity added later is refused without a code change.
- [ ] A regression test covers it alongside the existing `PATCH /policy` / `POST /attest` refusals, so
      the "demo viewer is read-only" claim has a test behind it rather than a docstring.
- [ ] `backend/identity/demo.py`'s docstring and `docs/status.html`'s "structurally read-only" claim
      are true again, or reworded to what is actually enforced.
- [ ] The junk household created by the measurement pass
      (`hh_2b15ef3a51b347e3bcdaa300820b9395`) is removed from Neon.

## Notes

- The measurement pass created that household. It is visible to `GET /households` for the demo
  viewer, so the public demo's picker shows it until it is deleted. Cleanup is a scoped two-row
  delete (`household_members`, then `households`) under the Neon **owner** string.
- This is a spawned ticket — the demo-prep verification found it, which is the ticket's own argument
  for running negative tests against production rather than trusting the local run.
