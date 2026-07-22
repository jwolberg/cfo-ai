---
id: "0058"
closed: 2026-07-22
revision: resfi-api-00007-cil
title: The public demo viewer can create a real household — POST /households has no plane gate
type: fix
status: done
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

- [x] `POST /households` with a demo-plane session is refused (403). — *Built 2026-07-21:
      `current_real_user` (`backend/identity/deps.py`) reads the new `users.is_demo` and refuses.*
- [x] …verified against the **deployed** API, not only locally. — *2026-07-22, revision
      `resfi-api-00007-cil`: `scripts/verify_demo_readonly.sh` all green, `POST /households` **403**.*
- [x] The refusal is structural — a new demo identity added later is refused without a code change.
      — *It reads the `users` row, not `DEMO_STYTCH_EMAIL`. `tests/test_demo_plane_gate.py` proves
      it with an identity that is neither the seeded viewer nor the configured email.*
- [x] A regression test covers it alongside the existing `PATCH /policy` / `POST /attest` refusals, so
      the "demo viewer is read-only" claim has a test behind it rather than a docstring.
      — *`tests/test_demo_plane_gate.py`, 7 tests.*
- [x] `backend/identity/demo.py`'s docstring and `docs/status.html`'s "structurally read-only" claim
      are true again, or reworded to what is actually enforced. — *Both true in production as of
      `resfi-api-00007-cil`. `status.html` needed no edit; it is published from `docs/` by
      `mobile/package.json`'s `copy:docs` and its claim is now accurate.*
- [x] The junk household created by the measurement pass
      (`hh_2b15ef3a51b347e3bcdaa300820b9395`) is removed from Neon. — *2026-07-22, two id-pinned
      deletes in one transaction, each asserting exactly one row. Done **before** migration `0015`
      — see the ordering note below.*
- [x] `POST /plaid/sync/now` refuses the demo plane at the identity layer. — *Spawned by verifying
      the deploy; it returned **500**, not 403. Fixed in `1c42968` and shipped in
      `resfi-api-00007-cil`.*

## What the fix does (2026-07-21)

The demo plane becomes a property of the **identity**, so it is answerable before any household
exists:

- **`0015_users_is_demo`** — `users.is_demo`, default false. Backfilled true for a user whose every
  membership is to a demo household (and who has at least one), so the definition is derived from the
  membership graph rather than an env var. A user with no memberships stays false — defaulting a
  JIT-provisioned user to true would lock real users out.
- **`current_real_user`** — `current_user` minus the demo plane, raising 403 "The demo session is
  read-only."
- Applied to the routes with no household to authorize against: `POST /households`,
  `POST /plaid/link/token`, `POST /plaid/link/exchange` — and, added 2026-07-22 after the deploy was
  measured, `POST /plaid/sync/now`. The link routes were the same hole — both are user-keyed — and
  refusing at `link/token` also stops a demo caller burning Plaid quota.
- `backend/seed.py` and `scripts/seed_demo_household.py` both assert the flag when they provision the
  demo viewer, so a fresh seed or re-import is safe without a manual step.

## Deployed 2026-07-22 — and the ordering that had to hold

Sequence run against production, in this order for a reason:

1. **Delete the junk household first.** `0015`'s backfill marks a user demo only when they have at
   least one demo membership and **no** non-demo one. The junk household was a non-demo household the
   demo viewer *owned*, so migrating with it in place would have backfilled the viewer `false`,
   `current_real_user` would never have fired, and the fix would have shipped **inert on a deployment
   that looked fixed**. Nothing in the deploy path re-asserts the flag: both seeders do
   (`backend/seed.py:230`, `scripts/seed_demo_household.py:101`), but `scripts/deploy_prod.sh` runs
   neither. Measured before: `demo=1 real=1 -> real`. After: `demo=1 real=0 -> DEMO`.
2. `alembic upgrade head`, owner credential, direct host — `0014` → `0015 (head)`.
3. `scripts/deploy_prod.sh` → `resfi-api-00006-vok`.
4. `scripts/verify_demo_readonly.sh` → one FAIL: `POST /plaid/sync/now` **500**. Fixed, tested,
   redeployed as `resfi-api-00007-cil`, re-verified **all green**.

Production held exactly one user (the demo viewer) at migration time, so the backfill had no chance
to sweep in a human account — worth recording because the seeded `user_reviewer_owner`, which is a
member of demo households and nothing else, *would* be marked demo by this predicate in any
environment where it exists.

## Notes

- The measurement pass created that household. It is visible to `GET /households` for the demo
  viewer, so the public demo's picker shows it until it is deleted. Cleanup is a scoped two-row
  delete (`household_members`, then `households`) under the Neon **owner** string.
- **Verifying the deploy is what found the second hole.** The same argument the ticket closes with,
  demonstrated twice: `POST /plaid/sync/now` was refused only in the route body, while
  `get_plaid_client` — a dependency, therefore built first — raised `PlaidNotConfigured` on a
  deployment with no Plaid credentials. A public 500 rather than a 403. It failed closed and wrote
  nothing, so it was hygiene rather than escalation, but no local run would have surfaced it: it
  needed a deployment configured exactly the way production is.
- This is a spawned ticket — the demo-prep verification found it, which is the ticket's own argument
  for running negative tests against production rather than trusting the local run.
