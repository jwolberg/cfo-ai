---
id: 0008
title: Identity is a membership graph — a platform users table and the households_for_user definer lookup
anchor: ADR-0008
status: accepted
date: 2026-07-18
supersedes:
superseded-by:
---

## [1] Context

[ADR-0004](./0004-postgres-scoped-by-household.md) made one rule not negotiable: **every table is
scoped by `household_id`, enforced at the repository layer and by Postgres row-level security.**
[ADR-0005](./0005-plaid-webhook-tenancy-exceptions.md) opened the first two bounded exceptions (an
unscoped raw webhook store and a `SECURITY DEFINER` lookup) and stated the rule that governs any
further one: ADR-0005 [3] — **a new definer function is a new ADR.**

Until now the isolation that RLS enforces has protected data whose *owner does not exist*. Every
route authenticates with one shared API key (`backend/auth.py`, `X-API-Key`) and then names the
household it wants; `household_id` arrives on the URL or in the request body and is trusted at face
value (`backend/main.py`, `backend/plaid/link.py`). `tests/test_idor.py` is explicit that the
mechanism is real and the identity is not. That is correct for synthetic households with no owner. It
breaks the moment a real balance is behind the door — which is exactly what linking a real bank means,
and is the premise that opened the identity rung
([`../plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md`](../plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md)).

This ADR records the three schema-level decisions that rung's first unit (ticket 0046) makes.

## [2] Decision

### [2.1] Identity is a membership graph, and `users` is a platform table

Two tables:

- **`users`** (`id`, `stytch_user_id` UNIQUE, `email`, `created_at`, `deleted_at`) — a verified user.
  Our `id` is the stable identifier the deferred Plaid Link rung will pass to `/link/token/create`
  as `client_user_id`; `stytch_user_id` is the vendor's, known only to the edge adapter.
- **`household_members`** (`household_id`, `user_id`, `role`, `created_at`) — the many-to-many bridge
  answering "which households may this user touch?". `role` is `owner` (may write) or `viewer`
  (read-only, the public demo principal).

**Users↔households is a membership join now, not a 1:1 owner column.** `backend/db/models.py` has
long flagged that "one household may eventually have two logins — a spouse's spending is precisely
what breaks a forecast." Retrofitting RLS from household-keyed to membership-keyed later is the
expensive path; the graph is built now even though the v1 UI is one user per household. Roles beyond
`owner`/`viewer` are deliberately deferred — that one distinction has a v1 consumer (the demo plane,
[2.4]) and richer permissions do not.

### [2.2] `users` is deliberately NOT household-scoped — a bounded, tested exception

A user exists before any household and independent of all of them, so `users` carries no
`household_id`, has no RLS policy, and is **outside `HOUSEHOLD_SCOPED`** — the same posture ADR-0005
took for the raw webhook store, and for the same reason (there is no household to scope to). Both
exclusions are named in `backend/db/models.py`'s `PLATFORM_TABLES` so each is an asserted decision,
not an omission: `tests/test_schema.py` and `tests/test_identity_schema.py` check that `users` is
outside the forced set, has no policy, and carries no `household_id`.

`users` carries PII (`email`) with no RLS backstop, so the discipline that replaces RLS is **single-key
access**: the application reads `users` only by `id` or `stytch_user_id` (`get_user_by_id`,
`get_user_by_stytch_id`), never an unfiltered scan or join, and there is no "list users" call. An
unfiltered scan is exactly the cross-user PII leak a later admin/console route could add by accident;
a test asserts the module exposes no such function and that a keyed read returns at most one row.

`household_members`, by contrast, **is** keyed by `household_id`, so it is HOUSEHOLD_SCOPED and
RLS-forced like every tenant table. `household_members.user_id` is a **bridge FK, not a tenant key** —
`test_no_table_carries_a_user_id_as_its_tenant_key` is relaxed to require that any table carrying
`user_id` also carries `household_id`, which is the precise statement of ADR-0004's rule.

### [2.3] Tenant resolution is a `SECURITY DEFINER` lookup — `households_for_user`

`household_id` becomes an **authorized selector, not a trusted assertion**: a request carries a Stytch
session, a dependency resolves it to our `user_id`, and the requested `household_id` must be in that
user's memberships or the route refuses (`403`). But the membership lookup has the same chicken-and-egg
as the webhook doorbell — we must read `household_members` to know which household to scope to, and it
is FORCE'd, so an unscoped app session sees nothing in it.

Resolved the same way ADR-0005 resolved the doorbell's: a narrow

```
households_for_user(p_user_id text) RETURNS SETOF text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
```

that mirrors `plaid_household_for_item` exactly — runs as its owner (a superuser/BYPASSRLS role in
every environment that runs migrations), returns nothing but household ids, and is `GRANT EXECUTE`'d to
`cfo_app`. This is preferred over granting the app role `BYPASSRLS`, which would unscope *every* query
the application makes. `assert_rls_binds()` continues to refuse a bypass-capable app role — the definer
function is the only audited surface that crosses scope, and it returns only ids.

### [2.4] `households.is_demo` — the demo plane's flag

An `is_demo` boolean (default false) marks the synthetic households. A `viewer`-role demo user is a
member of exactly the `is_demo` households and nothing else, so the public bundle reaches the demo with
no login while never touching a real household; and Plaid link-exchange (U3) refuses an `is_demo`
household, so a real bank item can never attach to the demo plane. The flag is backfilled true in
migration 0011 for the households already seeded, so the deployed demo keeps working with no re-seed.

## [3] Consequences

- **The shared key retires for user routes** (U3): user-facing routes require a verified session;
  internal machine routes keep their service/OIDC auth; the webhook keeps its Plaid-signed
  verification. The IDOR suite proves the *new* authority — a valid-but-non-member session is refused,
  not merely the old shared key.
- **One more definer function exists**, and per ADR-0005 [3] it is recorded here. There are now two:
  `plaid_household_for_item` and `households_for_user`. A third requires a fourth ADR.
- **The step-up-auth seam is designed, not built.** `authorize_household` is the single choke point a
  future sensitive action (loosening a guardrail, confirming a sweep) will demand a fresh factor at;
  this is why Stytch was chosen. Left as a documented extension point (KTD-9), enforced at money-on via
  a live-mode boot guard, not implemented here.
- **The Stytch secret** follows the Plaid/Method at-rest discipline, but its secrets-manager trigger is
  the first real (non-sandbox) signup, not the money-on trigger — identity goes live before money does.

## [4] Alternatives rejected

- **A 1:1 `owner_user_id` column on `households`.** Cheaper now, but the retrofit to a membership graph
  once a second login appears (a spouse) is the expensive path this product specifically expects to
  walk. Rejected in favour of building the join now.
- **Granting `cfo_app` BYPASSRLS for the membership lookup.** Unscopes every application query, exactly
  what ADR-0005 rejected for the doorbell. The definer function is the narrow alternative.
- **Putting `users` in `HOUSEHOLD_SCOPED`.** There is no household to scope a user to; it would force an
  artificial `household_id` on a platform entity and break JIT provisioning (a user exists before any
  membership). Rejected — the bounded, tested exception is honest.
