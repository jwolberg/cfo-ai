---
id: "0046"
title: The identity schema, the membership lookup, and ADR-0008
type: feat
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: []
created: 2026-07-18
---

# The identity schema, the membership lookup, and ADR-0008

Unit **U1** of the identity rung. It adds the two tables every later unit reads or writes, the
tenant-resolution function that makes `household_id` an *authorized* selector instead of a trusted
assertion, and the ADR that records why a second `SECURITY DEFINER` function is allowed.

Requirements: **KTD-1, KTD-2, KTD-3** (see the plan).

## What it is

Two new tables plus one function, in `alembic/versions/0011_identity.py`:

- **`users`** — `id` (our stable id), `stytch_user_id` (UNIQUE), `email`, `created_at`, `deleted_at`.
  A user exists independent of any household, so `users` is **platform-level, deliberately NOT in
  `HOUSEHOLD_SCOPED`** — the same posture as the FBO funding account. Its exclusion must be
  **explicit and tested**, not an oversight. Access is by `id`/`stytch_user_id` single-key lookup
  only — it carries PII with no RLS backstop, so no unfiltered scan/join may return more than one
  row.
- **`household_members`** — `(household_id, user_id, role, created_at)`, the many-to-many bridge.
  Keyed by `household_id`, so it **is** in `HOUSEHOLD_SCOPED` and RLS-forced. `role` is a
  CHECK-constrained `owner`/`viewer`; `viewer` is read-only (the public demo principal), `owner`
  may write.
- **`households_for_user(p_user_id text) RETURNS SETOF text`** — a `STABLE SECURITY DEFINER`
  function with `SET search_path = public, pg_temp`, returning the household ids a user belongs to.
  Mirrors `plaid_household_for_item` (0007, ADR-0005) exactly. Resolves the same chicken-and-egg the
  Plaid webhook has: we must read `household_members` to know which household to scope to, but it is
  FORCE'd — so a narrowly-scoped definer function, never `BYPASSRLS` on the app role.
- **`households.is_demo`** — a boolean (default false), set true on the seeded synthetic households.
  The flag KTD-10's demo plane and the link-exchange refusal both key on.

`users.id` is the **stable per-user identifier** the deferred Plaid Link rung will pass to
`/link/token/create` as `client_user_id`.

The migration hardcodes its own table names as literals and never imports `HOUSEHOLD_SCOPED`
(the 0033 trap). **ADR-0008** records the definer function + the platform-`users` exception + the
tenancy change.

## Files

- `alembic/versions/0011_identity.py` (new)
- `backend/db/models.py` (both tables; add `household_members` to `HOUSEHOLD_SCOPED`; assert `users`
  is deliberately excluded; `is_demo` on `households`)
- `backend/db/repository.py` (`add_user`, `get_user_by_stytch_id`, `add_membership`, a
  `households_for_user` caller, and the single-key `users` access guard)
- `backend/seed.py` (seed the demo `viewer` user as a member of every `is_demo` household — the
  public-demo principal — plus a dev/reviewer `owner` membership)
- `docs/decisions/0008-*.md` (new)
- `tests/test_idor.py` (seed real `household_members` rows per household)
- `tests/test_schema.py` (grants/RLS for the new tables + the `users` exclusion; **relax
  `test_no_table_carries_a_user_id` to scope the no-`user_id` rule to `HOUSEHOLD_SCOPED` tenancy
  keys** — `household_members.user_id` is a bridge FK, not the tenant key — **and drop `users` from
  `test_plaid_tables_do_not_exist_yet`'s not-yet-existing list**)
- `tests/test_identity_schema.py` (new)

## Acceptance criteria

- [ ] `users` created platform-level, keyed on `stytch_user_id` UNIQUE, NOT in `HOUSEHOLD_SCOPED`,
      no household RLS — and that exclusion is asserted, not incidental.
- [ ] `household_members` created RLS enabled AND forced, policy on `app.household_id`, granted to
      `cfo_app`, `role` CHECK-constrained to `owner`/`viewer`, added to `HOUSEHOLD_SCOPED`.
- [ ] `households_for_user` is STABLE SECURITY DEFINER with pinned `search_path`, returns only ids,
      and is refused to the app role outside its definer context.
- [ ] `households.is_demo` added (default false), true on seeded synthetic households.
- [ ] Migration up/down/up clean on a fresh DB from 0001; names its tables as literals (0033's rule).
- [ ] IDOR leak test is **non-vacuous**: real `household_members` rows seeded; household B reads zero
      of A's memberships.
- [ ] A test asserts `users` is only ever read by single-key lookup (no unfiltered scan returns >1
      row's PII).
- [ ] `docs/decisions/0008-*.md` accepted, recording the definer fn + the `users` exception + the
      tenancy change.

## Notes / risks

- The definer function is the only audited surface that crosses scope; `assert_rls_binds()` still
  refuses a bypass-capable app role.
- A from-zero migration CI job (0033's still-open follow-up) is the right thing to add alongside the
  three new migrations — flagged, not required by this ticket.
