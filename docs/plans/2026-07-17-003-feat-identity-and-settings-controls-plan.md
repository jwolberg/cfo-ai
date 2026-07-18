---
title: The identity rung — a real user, membership-scoped households, and the first write path (settings, attestation)
type: feat
status: reviewed
date: 2026-07-17
origin: this session's research (three repo/vendor explorers, 2026-07-17); premise raised by the user — "Plaid won't work without a real authed user and profile"
adr: docs/decisions/0008-*.md (owed by U1 — the `households_for_user` SECURITY DEFINER lookup + the platform-`users`-table RLS exception + the "household_id is authorized, not trusted" tenancy change)
---

# The identity rung — a real user, membership-scoped households, and the first write path

## Summary

Every route in this service trusts a **caller-supplied `household_id`** behind a **single shared API
key** that is baked into the client bundle (`backend/auth.py`; `EXPO_PUBLIC_API_KEY`). There is no
user, no owner, no session — `household_id` arrives on the URL or in the request body and is trusted at
face value (`backend/main.py:172`, `backend/plaid/link.py:36`). RLS genuinely isolates a household
*once one is chosen*, but **nothing verifies the chooser has any right to it** (`tests/test_idor.py`
is explicit that the mechanism is real and the identity is not). This is correct for synthetic data with
no owner; it breaks the moment a real balance is behind the door — which is exactly what linking a real
bank means.

This rung builds the missing precondition: **a verified user (Stytch), a membership graph mapping users
to households, and the derivation of `household_id` from the session instead of the request** — and then
uses that same new write-capable, per-user seam to close the two controls the engine already reads but
no screen writes: **policy settings** (`buffer_floor`, sweep caps, blackout/pause,
`min_days_between_sweeps`) and the **card-completeness attestation** (`0016` — the money-gate hardcoded
`attested=True` at `backend/precompute.py:878`).

**No production money moves and no real bank is linked in this rung.** The payment rail stays in shadow;
the Plaid *transport* stays Sandbox. What this rung delivers is the identity foundation the deferred
Link-UI/onboarding rung sits on top of, plus the product's **first real write path** — built with the
same append-only, RLS-forced, IDOR-fixture-tested discipline every read path already has.

Four decisions, resolved with the user on 2026-07-17, shaped this plan:

1. **Provider: Stytch.** Chosen over Clerk (the docs' stated intent) and Auth0 for the money-movement
   thesis — Stytch carries fraud/device intelligence and **step-up auth in the auth layer**, which is
   where a sensitive action (loosening a guardrail, later confirming a sweep) will eventually gate. The
   plan is ~90% provider-agnostic on the backend: Stytch touches only the edge token-verification
   adapter (KTD-4). **Reversal cost is not zero, though** — U6 also binds the mobile client to Stytch's
   Expo sign-in SDK, so switching providers later means replacing the backend adapter *and* the mobile
   sign-in flow. The bet is being made now, before KYC/step-up requirements are pinned; it is reversible,
   but at two integration points, not one.
2. **Users↔households is a membership join now, not a 1:1 owner column.** `backend/db/models.py:17-19`
   flags that "one household may eventually have two logins — a spouse's spending is precisely what
   breaks a forecast." Retrofitting RLS from household-keyed to membership-keyed later is the expensive
   path; the `users` + `household_members` graph is built now even though v1 UI is one user per
   household.
3. **The customer Plaid Link UI + onboarding is deferred to the next rung.** This rung ships the identity
   backbone + the settings/attestation write paths; a test user reaches the seeded demo households by
   **fixture membership** (mirroring how the sweep rung fixture-attested pilot households). Real
   signup → create household → link bank is the follow-on that consumes this rung's stable per-user id.
4. **The `0016` attestation write-path is in scope** as one of the controls — it is the named follow-up
   from both the transport and sweep rungs, and it is cheap once a user and a write path exist. Scope the
   claimed payoff honestly: within *this* rung it is exercised only against **fixture** households, so
   what lands here is the attestation mechanism (write + invalidation) built and shadow-tested. Its real
   payoff — measuring an actually-linked household, which today is silently refused as `UNATTESTED` — is
   **gated on the deferred Link rung**; this rung makes those households measurable *on arrival*, it does
   not itself measure one.

---

## Problem Frame

### The seam this rung replaces

Today a request authenticates with one shared secret (`backend/auth.py`, `X-API-Key`) and then *names*
the household it wants (`HouseholdId = Annotated[str, Path(...)]`, `backend/main.py:172`; a body field
for the assistant and for `POST /plaid/link/exchange`). `repository(engine, household_id)`
(`backend/db/repository.py:341`) binds `app.household_id` for RLS (`backend/db/session.py:211`), so the
row-level isolation is real — but the value it binds is unauthenticated request data. `GET /households`
lists **every** household with no scoping (`backend/main.py:345`). The module docstrings say this plainly
and on purpose: `backend/auth.py` ("a lock on a door, not an identity system"),
`backend/plaid/link.py:7` ("a real end-user flow would derive `household_id` from the authenticated
session instead of trusting the caller — that is the deferral").

There is no `users` table and no owner column on `households` anywhere in `alembic/versions/0001`–`0010`
(confirmed). Clerk/identity is named as future intent in `USERS.md`, `docs/architecture.md`, and the two
prior plans, always as "arrives with Plaid, with the first real token."

### Why the write half lives here too

The engine reads `UserPolicy` (`engine/models.py:591`: `buffer_floor`, `max_sweep`, `max_weekly_sweep`,
`blackout_dates`, `min_days_between_sweeps`) and a derived card-coverage attestation
(`engine/models.py:481`, `CoverageState`). Both have schema and plumbing — a `policies` table
(`backend/db/models.py:152`), a `Repository.set_policy()` (`backend/db/repository.py:144`), a
`derive_portfolio(..., attested)` parameter — but **no write path**: `set_policy` is called only by the
seeder (`backend/seed.py:282`), and `attested=True` is hardcoded (`backend/precompute.py:878`). The API
is GET-only by design (the sole non-GET app route, `POST /assistant/message`, writes nothing).

The reason settings and attestation cannot ship *before* identity is that a write is an action *by
someone, on their own household* — the exact thing that does not exist yet. Once the membership-scoped
`authorize_household` dependency exists (U2/U3), the write paths are small and follow it. Building them
in the same rung is why identity is worth building: it is not scaffolding, it is the unlock for the
product's first controls.

### What "done" means

No production money moves; no real bank is linked; the payment rail stays shadow and the Plaid transport
stays Sandbox. Done is: a **verified Stytch session** resolves to our `user`, whose **membership**
determines which households they may read or write; every user route **authorizes the household against
membership** (a non-member is refused even naming the id correctly); a user can **write their policy**
(audited, append-only) and **attest their card set** (clearing the `0016` money-gate in shadow); and a
**real Stytch sandbox** drives the whole path end to end (U7). A green *mocked* suite is explicitly not
evidence — "a mechanism built, tested, and never actually exercised" is this repo's recurring defect
class (tickets `0021`, `0033`, `0038`; the sweep rung's U6).

---

## Key Technical Decisions

### KTD-1 — Identity is a membership graph, and `users` is a platform table (not household-scoped)

Two new tables:

- **`users`** — `id` (our stable id), `stytch_user_id` (UNIQUE), `email`, `created_at`, `deleted_at`. A
  user exists independent of any household, so `users` is **platform-level, deliberately NOT in
  `HOUSEHOLD_SCOPED`** (`backend/db/models.py:452`) — the same posture as the FBO funding account in the
  sweep rung. Its exclusion must be **explicit and tested**: `assert_rls_binds()` machinery asserts the
  household-scoped set is FORCE'd; `users` being outside that set is an asserted decision, not an
  oversight. Access to `users` is by `user_id`/`stytch_user_id` key only.
- **`household_members`** — `(household_id, user_id, role, created_at)`, the many-to-many bridge. Keyed
  by `household_id`, so it **is** in `HOUSEHOLD_SCOPED` and RLS-forced like every other tenant table.
  `role` is **`owner` or `viewer`** and has a v1 consumer (KTD-10): `viewer` is read-only (the public
  demo principal), `owner` may write. Write routes require `owner`; this is the one role distinction the
  rung ships — richer per-permission roles stay deferred.

`users.id` is the **stable per-user identifier** the deferred Plaid Link rung will pass to
`/link/token/create` as `client_user_id` — this rung establishes it; the Link rung consumes it.

### KTD-2 — `household_id` becomes an *authorized selector*, not a trusted assertion

The route shapes stay (`/households/{household_id}/...`) so the deployed client and the reviewer
`HouseholdPicker` keep working — but the meaning of the path segment changes. The request carries a
Stytch session (`Authorization: Bearer …`); a dependency verifies it → resolves our `user_id` → looks up
the user's household memberships → **the requested `household_id` must be in that set, else `403`**. A
user with one household has one choice; the reviewer (a dev user with membership in all demo households)
still sees the picker. `household_id` is never again trusted from the wire without this check.

The membership lookup has the **same chicken-and-egg as the Plaid webhook**: we must read
`household_members` to know which household to scope to, but `household_members` is FORCE'd. Resolved the
same way — a narrowly-scoped `SECURITY DEFINER` function (KTD-3), never `BYPASSRLS` on the app role.

### KTD-3 — Tenant resolution is a `SECURITY DEFINER` lookup, per ADR-0005's rule → new ADR-0008

Add `households_for_user(p_user_id text) RETURNS SETOF text`, a `STABLE SECURITY DEFINER` function with
`SET search_path = public, pg_temp`, returning the household ids a user belongs to — mirroring
`plaid_household_for_item` (`alembic/versions/0007`, ADR-0005). ADR-0005 [3] states any new definer
function is a new ADR; **ADR-0008** records this function, the `users`-not-household-scoped exception
(KTD-1), and the tenancy change (KTD-2). `assert_rls_binds()` continues to refuse a bypass-capable app
role — the definer function is the only audited surface that crosses scope, and it returns nothing but
ids.

### KTD-4 — Stytch is a thin verification adapter at the edge, mirroring the Plaid webhook JWT path

Verify the Stytch session **locally against Stytch's JWKS** (no per-request round-trip), exactly as
`backend/plaid/webhook.py:61-89` already verifies Plaid's ES256 JWT: fetch+cache the signing key by
`kid`, `jwt.decode(..., algorithms=[…], options={"require": ["iat","exp"]})`, reject on bad
signature/alg/expiry. A `backend/identity/` module isolates the vendor: `stytch.py` (verify token →
`stytch_user_id` + claims), `deps.py` (`current_user`, `authorize_household`), and the JWKS cache. Only
`stytch.py` knows the word "Stytch"; swapping providers later swaps this one file. The Stytch **secret**
follows the Plaid/Method credential discipline (KTD-7 of the sweep rung): env-flag guard now, never
logged, sandbox and production keys never interchangeable — but its **secrets-manager trigger is the
first real (non-sandbox) Stytch project / first real signup, NOT the money-on trigger** the Plaid/Method
keys share. The asset it protects is real user identity and session issuance, which goes live the moment
real users exist, independent of whether the financial rail is on; a leaked Stytch secret forges real
sessions even while every balance is synthetic.

**Session lifecycle (revocation, TTL, key rotation).** Local JWKS verification means a token is trusted
on signature + expiry alone, with no per-request server check — so the plan must bound how long a stale
trust survives. Default: **short session TTL + refresh**, so logout / device-compromise / admin-forced
revocation take effect within the TTL rather than at a distant expiry (confirm Stytch's session shape —
short-JWT-plus-refresh vs. long opaque session requiring introspection — per the Deferred Notes; if it
is the latter, local-only verification is reconsidered). And unlike Plaid's single-use webhook JWTs, a
session key is verified repeatedly over a session's life, so the **JWKS `kid` cache needs TTL-based
eviction** (not the never-evict Plaid pattern), so a rotated or compromised signing key stops being
trusted within a bounded window, not just on process restart.

### KTD-5 — JIT provisioning; households reached by fixture membership this rung

On the first verified session bearing an unknown `stytch_user_id`, insert a `users` row (just-in-time
provisioning) — signup is a Stytch concern, not a route we build. Because onboarding (create-your-own-
household) is deferred, a test/dev user reaches the **seeded demo households** through **fixture
`household_members` rows** (a seed/dev path, mirroring the sweep rung's fixture attestation). This keeps
the deployed reviewer surface alive without the shared key and without building onboarding. Real
"create a household on signup" is the deferred Link rung.

### KTD-6 — Policy is append-only (source of record), current state derived — matching the whole codebase

The codebase's identity is append-only auditability: `decisions` store their entire frozen input,
`transfers` are append-only by grant, state is a projection. Policy is safety-critical (it is the
guardrail set), and the admin story already calls for "every action logged to the same append-only
record as a decision" (`status.html`). So the source of record becomes an **append-only `policy_events`**
table (`household_id`, `seq` (monotonic identity), `changed_by` user_id, the full policy snapshot,
`created_at`), and `Repository.policy()` reads the **latest event per household** — ordered by `seq`,
not `created_at`, since a seed transaction can tie on `created_at` (the sweep rung's `transfers.seq`
lesson) — instead of a mutable row. This honors the
"store only what cannot be derived; don't keep a second copy that can disagree" prior — history cannot
be derived from current state, but current state *can* be derived from history. The mutable `policies`
upsert (`repository.py:144`) is retired in favor of the append; the seeder writes an initial event.
*(Tradeoff logged: this touches the existing policy read path and the seeder. The alternative —
mutable `policies` + a separate audit table — keeps a second copy that can disagree, which the prior
rejects. Append-only is the smaller conceptual surface even though it edits more lines.)*

### KTD-7 — Attestation is append-only and card-set-scoped; `UNMATCHED_PAYMENT` still overrides

Replace the hardcoded `attested=True` (`precompute.py:878`) with a read of a real per-household
attestation. Add an append-only **`card_attestations`** table (`household_id`, `attested_by`,
`card_fingerprint`, `created_at`), where `card_fingerprint` is a stable hash of the attested card set.
`assemble_snapshot()` computes `attested = (a current attestation exists whose fingerprint matches the
current card set)` — so **a newly appearing card silently invalidates a stale attestation** and drops
the household back to `UNATTESTED`, which is the correct safety behavior. Crucially, attestation only
moves `UNATTESTED → COMPLETE`; it **never overrides `UNMATCHED_PAYMENT`** (`engine/models.py:481`,
`derive_portfolio`) — a user cannot attest away a card-shaped outflow to a card we cannot see. The route
is `POST /households/{household_id}/attest`, membership-authorized (KTD-2).

### KTD-8 — The shared key retires; two planes replace it — a public demo and authed real users

The `RESFI_API_KEY` is **retired for user routes**, not demoted to a lingering second authority. After
cutover, **user-facing routes require a verified Stytch session** (KTD-2). **Internal machine routes**
(the Plaid sync worker, reconcile, any Cloud Tasks/Scheduler target) keep their existing service/OIDC
gating; the Plaid webhook doorbell keeps its Plaid-signed verification. The IDOR suite proves the *new*
authority (a valid-but-non-member session is refused), not just the old one.

The deployed public demo does **not** die with the shared key — it becomes its own plane (KTD-10), which
is also what lets the cutover happen without a maintenance window: the demo runs on a public read-only
demo principal while real users run on their own sessions, so there is never a window where the deployed
surface has no working auth.

### KTD-9 — The step-up-auth seam is designed now, built at money-on

Money is not moving, so no MFA/step-up is built here. But `authorize_household` is the single choke point
where a future sensitive action (loosening a guardrail below its current value, attestation, and
eventually confirming a sweep) will demand a fresh factor. The dependency is shaped to make that a
later addition at one seam — this is *why* Stytch was chosen (step-up in the auth layer). Left as a
documented extension point, not implemented.

**Guardrail-loosening ships ungated this rung — deliberately, and with two compensating controls.** U4's
`PATCH /policy` can lower `buffer_floor` or raise a sweep cap, the exact action named above. It ships
gated only by session + membership because **shadow mode nulls its financial impact** (no sweep executes).
The two controls that make that acceptance safe rather than silent: (1) a policy change that *loosens* a
guardrail is **flagged distinctly in `policy_events`** (an audit marker), so the eventual step-up
retrofit and any review can find them; and (2) the **live-mode boot guard** — when `TRANSFER_MODE`
flips to live, startup must refuse to boot unless step-up is wired for loosening writes, mirroring
`assert_transfer_credentials_safe_at_rest`. So the gate is *designed and asserted* now, *enforced* at
money-on.

### KTD-10 — The public demo is a plane, not an exception: a read-only viewer over synthetic households

The demo at `cfo-ai-1.web.app` stays **public and unauthenticated to the visitor**, in parallel with real
authed users — implemented as a scoped principal, not a hole:

- Seeded/synthetic households carry an **`is_demo` flag**. A **demo user** is a `viewer`-role member
  (KTD-1's `role` column, finally with a v1 consumer) of **exactly** the `is_demo` households. The web
  bundle carries this demo session, so a visitor reaches the demo with no login; a real user signs in and
  gets their own `owner` session over their own household.
- **`role` gates writes.** `PATCH /policy`, `POST /attest`, and Plaid link-exchange require the
  `owner` role — the demo viewer is structurally read-only, not read-only-by-convention. This is the
  same authz seam (`authorize_household`), extended with a role check; no separate route family.
- **The two planes cannot cross.** The demo viewer is a member of *only* `is_demo` households, so it can
  never reach a real one; and link-exchange refuses an `is_demo` household, so a real bank item can never
  attach to the demo plane. Real user data is never behind the public session, and synthetic data is
  never behind a real one.
- The demo session is **not** the old shared key: it is a real, scoped, read-only, revocable principal
  that carries no ability to write or to name a non-demo household.

---

## High-Level Technical Design

*Directional, for review — not implementation specification.*

### Request flow, before and after

```
BEFORE:  request + shared X-API-Key  ──►  household_id from URL/body (trusted)  ──►  RLS scope  ──►  data
AFTER:   request + Stytch session    ──►  verify (JWKS)  ──►  our user_id
                                      ──►  households_for_user(user_id)  [SECURITY DEFINER]
                                      ──►  requested household_id ∈ memberships?  ── no ─►  403
                                                    │ yes
                                                    ▼
                                            RLS scope (unchanged)  ──►  read OR write
```

### The identity module

```
backend/identity/
  stytch.py   verify(session_token) -> (stytch_user_id, claims)   # local JWKS verify, mirrors plaid/webhook.py
  deps.py     current_user()        -> User                       # verify + JIT-provision
              authorize_household(household_id) -> ScopedRepo      # membership check (SECURITY DEFINER) then RLS scope
  (only stytch.py names the vendor; deps.py is provider-agnostic)
```

### New schema (migrations 0011–0013, next revisions after 0010; confirm at implementation)

```
users               (platform, NOT household-scoped): id, stytch_user_id UNIQUE, email, created_at, deleted_at
household_members   (HOUSEHOLD_SCOPED, RLS-forced):    household_id, user_id, role, created_at
households_for_user(user_id) -> setof household_id      SECURITY DEFINER   [ADR-0008]

policy_events       (HOUSEHOLD_SCOPED, append-only):   household_id, seq (identity), changed_by, <policy snapshot cols>, created_at
card_attestations   (HOUSEHOLD_SCOPED, append-only):   household_id, attested_by, card_fingerprint, created_at
```

### New write routes (the product's first user-driven writes)

```
PATCH /households/{household_id}/policy    body = the guardrail set; validate; append policy_event
POST  /households/{household_id}/attest    body = the attested card set; append card_attestation
       (both: authorize_household → RLS scope → append; membership-gated, audited)
```

---

## Prerequisites / Dependencies

- **A Stytch account + sandbox keys** for U2/U7 (skip the real-vendor gate loudly via `skipif` when
  absent, like `tests/test_plaid_sandbox.py`). The production key is a secrets-manager item (KTD-4).
- **The seeded demo households** already exist; U1/U3 add fixture `household_members` linking a dev user.
- **A from-zero migration CI job** — ticket `0033`'s still-open follow-up (no CI job migrates a fresh DB
  to head, so the `HOUSEHOLD_SCOPED`-import class of bug recurs silently). Three new migrations is the
  right moment to finally add it (flagged, not required).
- **No merge predecessor** — `main` already carries `backend/plaid/`, the transfers rung, and ADR-0007.
- **The live-request assembly path is a named, unresolved dependency (not built here).** Nothing today
  reads `Repository.policy()` or a live attestation into `decide()` for a real request — the demo
  `assemble_snapshot()` uses a hardcoded `UserPolicy` and `readpath.py` serves frozen precomputed
  snapshots. So U4/U5 writes are provably persisted, audited, and read back, but a *live decision* reflects
  them only once a live per-household assembly path exists. This rung does not build that path; it either
  becomes its own follow-up unit or is pulled in explicitly. (The sweep rung's plan flagged this identical
  gap and left it open — see `docs/plans/2026-07-17-002-...-plan.md`.)

---

## Implementation Units

*One ticket and one commit per unit (per project rules). Eight units (U6 split into U6a/U6b per the
scope review). Tickets `0046`–`0053` (confirm next id).*

### U1. The identity schema, the membership lookup, and ADR-0008

**Goal.** `users` (platform) + `household_members` (household-scoped) + the `households_for_user`
SECURITY DEFINER function, with RLS/grants, the `users`-exclusion assertion, and non-vacuous IDOR
fixtures — plus ADR-0008.

**Requirements.** KTD-1, KTD-2, KTD-3.

**Files.** `alembic/versions/0011_identity.py` (new); `backend/db/models.py` (add both tables; add
`"household_members"` to `HOUSEHOLD_SCOPED`; assert `users` is deliberately excluded);
`backend/db/repository.py` (`add_user`, `get_user_by_stytch_id`, `add_membership`, a
`households_for_user` caller, and — since `users` carries PII with no RLS backstop — a guard that all
`users` access is a single-key lookup by `id`/`stytch_user_id`, never an unfiltered scan/join, so a
later admin/console route can't leak cross-user PII); `backend/seed.py` (seed the **demo `viewer` user**
as a member of every `is_demo` household — the public-demo principal of KTD-10 — plus any dev/reviewer
`owner` membership; the deployed-surface mitigation, not just a test fixture); `docs/decisions/0008-*.md`;
`tests/test_idor.py` (seed real
`household_members` rows per household); `tests/test_schema.py` (grants/RLS for the new tables + the
`users` exclusion; **relax `test_no_table_carries_a_user_id` to scope the no-`user_id` rule to
`HOUSEHOLD_SCOPED` tenancy keys — `household_members.user_id` is a bridge FK, not the tenant key — and
drop `users` from `test_plaid_tables_do_not_exist_yet`'s not-yet-existing list**);
`tests/test_identity_schema.py` (new).

**Approach.** `users` platform-level, no household RLS, keyed on `stytch_user_id`. `household_members`
RLS-forced two-statement policy keyed on `app.household_id`; `role` is a CHECK-constrained
`owner`/`viewer`. Add an **`is_demo` boolean to `households`** (default false), set true on the seeded
synthetic households — the flag KTD-10's demo plane and the link-exchange refusal both key on.
`households_for_user` mirrors `plaid_household_for_item` exactly (STABLE, SECURITY DEFINER, pinned
`search_path`, returns only ids). The migration hardcodes its own table names as literals and never
imports `HOUSEHOLD_SCOPED` (the `0033` trap). ADR-0008 records the definer function + the platform-`users`
exception + the tenancy change.

**Test scenarios.** `household_members` leak test is **non-vacuous** (real rows seeded) — household B
reads zero of A's memberships; `households_for_user` returns exactly a user's households and is refused
to the app role outside its definer context; `users` is reachable by key but carries no household RLS;
`assert_rls_binds`-style check confirms `users` is intentionally outside the forced set; a test asserts
`users` is only ever read by single-key lookup (no unfiltered scan can return more than one row's PII); a
fresh-DB migration reaches head.

**Verification.** `test_idor`/`test_schema` pass with the new tables; the definer lookup resolves a user
to their households and nothing more; ADR-0008 accepted.

---

### U2. The Stytch verification adapter + `current_user` / `authorize_household`

**Goal.** `backend/identity/` — local JWKS session verification, JIT user provisioning, and the two
FastAPI dependencies, with a real-Stytch sandbox gate that skips loudly.

**Requirements.** KTD-2, KTD-4, KTD-5, KTD-9.

**Dependencies.** U1.

**Files.** `backend/identity/__init__.py`, `stytch.py`, `deps.py`; `backend/db/session.py` (the Stytch
secret guard, mirroring `assert_plaid_tokens_safe_at_rest`); `tests/test_identity_deps.py` (stubbed
verifier); `tests/test_identity_stytch_sandbox.py` (new — `skipif` without sandbox creds).

**Approach.** `stytch.py` verifies the session token against Stytch's JWKS the way
`backend/plaid/webhook.py` verifies Plaid's — cache the key by `kid`, decode with required `iat`/`exp`,
reject bad alg/signature/expiry/replay. `current_user` verifies and JIT-provisions on unknown
`stytch_user_id`. `authorize_household(household_id)` calls `households_for_user`, raises `403` on
non-membership, and otherwise yields a household-scoped repository (composes with the existing
`repository()` seam). `deps.py` names no vendor. The secret guard fails closed in a production config with
a plaintext credential.

**Test scenarios.** A tampered/expired/replayed/ wrong-alg token is rejected before any DB touch; a valid
token JIT-provisions a `users` row once (idempotent on second login); `authorize_household` yields a
scoped repo for a member and `403`s a valid non-member; the secret guard raises in a simulated production
config; the Stytch secret never appears in logs.

**Verification.** The dependencies gate a throwaway route correctly under a stubbed verifier; the
sandbox gate skips loudly with its full reason.

---

### U3. Cut the API over — session-derived, membership-authorized household (fixes the Plaid-link premise)

**Goal.** Every user route requires a verified session and authorizes the household against membership;
internal routes keep service auth; `POST /plaid/link/exchange` derives `household_id` from the session
instead of the request body — the direct fix to the premise that started this rung.

**Requirements.** KTD-2, KTD-8; the premise ("Plaid won't work without a real authed user").

**Dependencies.** U1, U2.

**Files.** `backend/main.py` (swap `require_api_key` for `authorize_household` on the household routes;
scope `GET /households` to the caller's memberships instead of listing all; **also fix
`POST /assistant/message` — its `AssistantRequest.household_id` is the *other* body-supplied,
caller-trusted household id (Problem Frame), so it must authorize that id against membership too, or it
stays the one un-fixed IDOR after cutover**; `GET /households` has no path id, so it authorizes via
`current_user` + `households_for_user` directly rather than `authorize_household`); `backend/plaid/link.py`
(drop `household_id` from `LinkExchangeRequest`; derive it from `current_user` membership);
`backend/auth.py` (document/retire the shared key's new role, KTD-8); `tests/test_idor.py` and a new
`tests/test_route_authz.py` (valid-but-non-member refusal; `GET /households` returns only the caller's).

**Approach.** `GET /households` becomes "your households" (membership-scoped). Reads are allowed for any
member role; **writes require `owner`** (KTD-10) — the public demo `viewer` can read every demo household
but can write none. `link_exchange` takes no `household_id` from the wire (a user links to *their own*
household) **and refuses an `is_demo` household**, so a real bank item can never attach to the demo plane.
The public demo runs on the pre-seeded read-only demo session (KTD-10), so the deployed surface keeps
working across the cutover with no shared key and no maintenance window. Internal routes
(`/plaid/sync/worker`, `/plaid/sync/poll`, any Cloud Tasks target) keep OIDC/service auth untouched; the
webhook doorbell keeps Plaid signature verification.

**Test scenarios.** A valid session naming a non-member household is refused on **every** household route
(read and, later, write); `GET /households` never returns a non-member household; `link_exchange` cannot
pin an item to a household the caller is not a member of (the request has no `household_id` to smuggle),
**and refuses an `is_demo` household outright**; the **demo `viewer` session reads a demo household but is
`403`'d on any write** (`PATCH /policy`, `POST /attest`); the demo viewer cannot see a non-demo household;
internal routes still authenticate by service credential, not session.

**Verification.** The deployed-shape client flow works end to end for a member; cross-user isolation
holds under a valid-but-non-member session; the Plaid link path no longer trusts a wire `household_id`.

---

### U4. The settings write path — append-only `policy_events`, current state derived

**Goal.** `PATCH /households/{household_id}/policy` writes a validated, audited, append-only policy change;
`Repository.policy()` reads the latest event; the seeder writes an initial event.

**Requirements.** KTD-6; `engine/models.py:591` (the `UserPolicy` invariants).

**Dependencies.** U1–U3.

**Files.** `alembic/versions/0012_policy_events.py` (new; add `"policy_events"` to `HOUSEHOLD_SCOPED`,
append-only grant SELECT+INSERT; **and a data-migration step that backfills one `policy_events` row per
existing `policies` row before the mutable upsert is retired — otherwise every already-seeded household,
including the deployed reviewer surface, reads zero events and loses its policy after cutover**);
`backend/db/repository.py` (`policy()` reads latest event;
`set_policy` becomes an append; retire the mutable upsert); `backend/main.py` (the `PATCH` route +
validation); `backend/seed.py` (write an initial event); `backend/precompute.py` (unchanged read
contract — still gets a `UserPolicy`); `tests/test_policy_write.py` (new); update any test asserting the
old mutable `policies` shape.

**Approach.** Validate against `UserPolicy.__post_init__` plus explicit bounds (`buffer_floor ≥ 0`, caps
`≥ MIN_SWEEP` and internally consistent, `min_days_between_sweeps` in a sane range). Append a
`policy_event` under `authorize_household` + RLS scope; `policy()` selects the latest by `seq`
per household. *(Follow the sweep rung's `seq` lesson — order by a monotonic column, not `now()`, since a
seed transaction can tie on `created_at`.)*

**Test scenarios.** Write→read roundtrip returns the new guardrails; an invariant violation (negative
buffer, cap below `MIN_SWEEP`, negative spacing) is rejected with no row appended; a non-member `403`s;
the audit trail preserves prior values (history is non-destructive); cross-household scope holds;
`Repository.policy()` returns the newly written policy on the next read.

**Verification.** A user changes their buffer floor; `Repository.policy()` returns the new guardrails and
the prior value remains in `policy_events`. *(Caveat — no code path today reads `Repository.policy()` back
into `decide()` for a live household: the demo `assemble_snapshot()` takes a hardcoded `UserPolicy` and
`readpath.py` serves a frozen precomputed snapshot. So this rung proves the write + audit + latest-read,
**not** a re-decided sweep. Making the write actually change a live decision needs the live-assembly path
named in Prerequisites; the sweep rung's own plan flagged this same gap unresolved.)*

---

### U5. The attestation write-path (`0016`) — the money-gate, in shadow

**Goal.** Replace the hardcoded `attested=True` with a real, card-set-scoped, append-only attestation a
user can set; `POST /households/{household_id}/attest` records it; a new card invalidates a stale attestation;
`UNMATCHED_PAYMENT` still overrides.

**Requirements.** KTD-7; `0016`; `engine/models.py:481` (`CoverageState`); `precompute.py:756,878`.

**Dependencies.** U1–U3.

**Files.** `alembic/versions/0013_card_attestations.py` (new; `HOUSEHOLD_SCOPED`, append-only);
`backend/db/repository.py` (`add_attestation`, `current_attestation`); `backend/precompute.py`
(`assemble_snapshot()` computes `attested` from a current fingerprint-matching attestation instead of
`True`); `backend/main.py` (the `POST /attest` route); `tests/test_attestation.py` (new).

**Approach.** `card_fingerprint` is a stable hash over the attested card identities. `assemble_snapshot()`
reads "does a current attestation match the current card set" → `attested` bool into `derive_portfolio`.
A newly appearing card changes the fingerprint → `UNATTESTED` again (correct). Attestation moves only
`UNATTESTED → COMPLETE`; `detect_unmatched_payments` producing `UNMATCHED_PAYMENT` is never overridable.

**Test scenarios.** Attest → coverage `COMPLETE` and the money-gate clears (in shadow); an unattested
household is refused `CARD_COVERAGE_INCOMPLETE`; a household with an unmatched card payment **cannot** be
attested to `COMPLETE`; adding a card re-triggers `UNATTESTED`; a non-member `403`s; cross-household
scope holds.

**Verification.** A fixture-linked real-shaped household is attested by its member and the engine stops
refusing it for coverage on the `assemble_snapshot()` path U5 edits — the exact `0016` gap ("no one has
ever cleared it end to end") closed in shadow. *(Same live-serving caveat as U4: `readpath.py` serves
frozen snapshots, so a live decision reflects the attestation only once its snapshot is re-assembled —
see the live-assembly Prerequisite.)*

---

### U6a. Mobile — session auth cutover (drop the baked key)

**Goal.** Replace the baked shared key + `DEMO_HOUSEHOLD` with a Stytch session and the user's
membership-scoped households — the highest-value, lowest-risk half of the mobile work, split out (per the
scope review) so it ships as soon as U3 lands rather than waiting on the two write screens.

**Requirements.** KTD-2, KTD-4; the premise (no shared key in the client bundle).

**Dependencies.** U3 only.

**Files.** `mobile/src/api/client.ts` (send the session token, drop the static key + `DEMO_HOUSEHOLD`
default); a minimal Stytch Expo sign-in; `mobile/src/components/HouseholdPicker.tsx` (becomes "your
households" from membership); `mobile/src/screens/NoHousehold.tsx` (new — see empty-state); tests under
`mobile/`.

**Approach.** Sign-in is Stytch's Expo SDK; the client attaches the session and reads the user's
households. **The public demo build carries the pre-seeded read-only demo `viewer` session (KTD-10)** —
the visitor reaches `cfo-ai-1.web.app` with no login, exactly as today, while a real user signs in to get
their own `owner` session; the two planes never cross. **The session token is stored in Expo SecureStore
/ Keychain — never `AsyncStorage` or plain storage — and never logged** (it is a higher-value bearer
credential than the shared key it replaces; mirror KTD-4's secret discipline). **Empty state:** a freshly
JIT-provisioned user with zero memberships
(a second reviewer, a QA tester — the default outcome given fixtures are narrow) lands on a graceful
`NoHousehold` screen ("You don't have access to a household yet"), *not* a blank screen and *not* a
signup flow (onboarding is deferred). Single-membership users skip the picker; multi-membership (the
reviewer) still sees it.

**Test scenarios.** A signed-in member sees only their household(s); a signed-in user with **zero**
memberships sees the `NoHousehold` screen, not a crash or blank; no static API key remains in the bundle;
the session token is read from SecureStore, never logged.

**Verification.** The deployed web build works for a signed-in member with no baked key; the reviewer dev
user still sees all demo households; the zero-membership path is handled.

---

### U6b. Mobile — the Settings + Attestation write screens

**Goal.** The product's first user-facing write screens: Settings (guardrails + pause) writing via U4,
Attestation ("confirm your cards") writing via U5 — with their interaction states specified, not left to
the implementer.

**Requirements.** The write paths (U4/U5); U6a (session + household context).

**Dependencies.** U6a, U4, U5.

**Files.** `mobile/src/screens/Settings.tsx` (new); `mobile/src/screens/Attest.tsx` (new); a CTA on the
existing read-only feed item into `Attest.tsx`; tests under `mobile/`.

**Approach.** Keep the screens minimal — this is where scope balloons.

- **Settings states (the first write — no silent no-ops):** map U4's server-side validation bounds to
  **inline per-field errors**; show a **saving/disabled** state during the `PATCH`; confirm an
  **explicit success** after write (a guardrail edit the user can't confirm is a safety problem).
- **Pause model (name it, don't hand-wave):** "Pause" is UI over `blackout_dates`. Support **today-only,
  a dated range, and indefinite-until-cleared**; **list active blackout entries and let the user remove
  one to un-pause**; enabling pause takes a **lightweight confirm** (it stops money movement). Copy
  follows the engine's `BLACKOUT` semantics.
- **Attest, and the unmatched-payment dead end:** a household in `UNMATCHED_PAYMENT` **cannot** be
  attested (KTD-7). Attest shows a **distinct** state from plain "not yet attested" — it names that a
  card the system can't match is blocking, and that there is **no in-app fix this rung** (points to
  support / the next-rung Link UI), rather than silently failing the write.
- **Discoverability:** the existing feed's `CARD_COVERAGE_INCOMPLETE` refusal item gets a **CTA into
  `Attest.tsx`** (the feed data stays read-only) — otherwise the user has no way to reach the screen that
  clears the refusal they're staring at.

**Test scenarios.** A settings change persists and the dashboard reflects the new guardrail; an invalid
edit shows an inline error and no write; pause today/range/indefinite each persist and are individually
removable; attesting a clean household clears the coverage refusal in the feed; a household with an
unmatched payment shows the distinct blocked state and the write is refused, not silently dropped.

**Verification.** A member can set policy (with visible validation/save states), manage pause, and attest;
the unmatched-payment and coverage-refusal flows both have a defined, non-dead-end UX.

---

### U7. The real-Stytch end-to-end gate (the unit that makes the rest real)

**Goal.** Drive the whole path against a **real Stytch sandbox** — verify a real session, resolve to
fixture households, `403` a non-member, write an audited policy change, attest a household (money-gate
clears in shadow), and prove cross-user isolation.

**Requirements.** The repo's "built, tested, never exercised" discipline; the sweep rung's U6 as the
template.

**Dependencies.** U1–U6b.

**Files.** `tests/test_identity_sandbox.py` (new — the hard gate; `skipif` without Stytch sandbox creds,
loudly); confirm `tests/test_idor.py`'s membership leak test is non-vacuous.

**Approach.** Use real Stytch sandbox sessions, not a stubbed verifier: mint a session for a sandbox
user, exercise `current_user` JIT provisioning, `authorize_household` membership + `403`, a policy
`PATCH` (assert the audit trail), an `attest` (assert coverage → `COMPLETE`), and a second sandbox user
proving isolation. Document provenance ("green against real Stytch sandbox on <date>") and any
vendor-reality correction (budget for ≥1, as the Plaid and sweep rungs each hit). A green **mocked**
suite is explicitly **not** accepted as evidence.

**Test scenarios.** Real session verifies and provisions; a real non-member session is refused on every
household route; the policy write and attestation write are visible and audited; two sandbox users are
mutually invisible; the suite skips loudly without creds.

**Verification.** The gate runs green against the real Stytch sandbox including a cross-user isolation
check; provenance and any correction recorded.

---

## Scope Boundaries

### In scope
The eight units: the identity schema + membership lookup (ADR-0008), the Stytch adapter + dependencies,
the API cutover (including the Plaid-link `household_id` fix), the append-only settings write path, the
attestation money-gate, the mobile session cutover (U6a) and the Settings/Attest write screens (U6b), and
the real-Stytch gate.
No production money; no real bank; the rail stays shadow, the transport stays Sandbox.

### Deferred to the next rung (the user's Decision 3)
- **Customer Plaid Link UI + onboarding** — signup → create your household → link your bank end to end.
  It consumes this rung's stable `users.id` as Plaid's `client_user_id` and the `/link/token/create`
  call that does not exist yet.

### Deferred for later (event-triggered)
- **KYC / AML identity verification** (Persona/Alloy/Socure/Plaid IDV) — a distinct problem from authN;
  trigger: the first real money movement, not the first login.
- **Step-up / adaptive MFA** — the `authorize_household` seam is designed for it (KTD-9); trigger: money
  on (loosening a guardrail, confirming a sweep).
- **KMS for the Stytch secret** — secrets-manager discipline, but triggered at **first real signup**, not
  the Plaid/Method money-on trigger (KTD-4): identity is live before money is.
- **The operator console, overrides/kill-switch, access-and-audit admin surface** — its own rung.
- **Native app-store presence and push** — deploy targets iOS/Android from the same source; separate work.

### Outside this product's identity
- **Roles beyond `owner`/`viewer`** — this rung ships exactly that one distinction (writes need `owner`;
  the demo is `viewer`, KTD-10). Richer authorization (spousal permissions, per-action grants) is a
  later refinement, not a v1 gate.

---

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A user authorized for the wrong household (the IDOR the whole rung exists to prevent) | Low if guarded | Severe | KTD-2 membership check via the `households_for_user` definer lookup; the valid-but-non-member refusal test is written first (U3) and is a hard gate (U7). |
| The new authz is "built, tested, never exercised" (repo's recurring class) | Medium (history) | High | U7 drives a **real** Stytch sandbox; the membership leak test is seeded non-vacuous (U1). |
| Cutover breaks the deployed public demo | Medium | Medium | The demo becomes its own plane (KTD-10) — a public read-only `viewer` session over `is_demo` households — so the deployed surface keeps working with no shared key and no maintenance window; U6a splits the auth cutover ahead of the write screens. |
| The demo plane leaks into (or from) real data | Low | Severe | The demo `viewer` is a member of *only* `is_demo` households and can write nothing (`owner`-gated); `link_exchange` refuses `is_demo` households, so a real bank item never lands on the demo plane. Both directions IDOR-tested (U3). |
| The definer function widens RLS bypass | Low | High | Narrowly scoped, returns only ids, ADR-0008; `assert_rls_binds` still refuses a bypass-capable app role. |
| Stytch session verification flaw (replay, expiry, JWKS rotation) | Medium | Severe | Reuse the proven Plaid-webhook JWT verification shape (require `iat`/`exp`, reject bad alg/replay); U7 exercises real tokens. |
| Policy read-path migration (append-only) regresses the engine input | Medium | Medium | `Repository.policy()` keeps returning a `UserPolicy`; order by a monotonic seq not `now()` (sweep-rung lesson); a write→decide test pins it (U4). |
| Mobile scope balloons (onboarding creep) | Medium | Medium | Screens kept minimal; onboarding/Link UI explicitly out (Decision 3); fixture membership replaces a signup flow this rung. |

---

## Deferred Implementation Notes (execution-time unknowns)

- **Stytch session shape** — confirm whether Stytch issues a verifiable JWT (local JWKS verify preferred)
  or requires a `sessions/authenticate` round-trip; size the per-request cost and default to local verify
  with JWKS caching (the Plaid pattern) if available.
- **`client_user_id` continuity** — record that the deferred Link rung must pass a **stable** per-user id
  to `/link/token/create`; `users.id` is that id, established here.
- **`role` semantics** — ships as `owner`/`viewer` only (KTD-10's demo plane is the consumer); do not
  over-model richer permissions until the admin/permissions rung needs them.
- **From-zero migration CI** — three new migrations is the moment to close ticket `0033`'s follow-up.

---

## Review History

*Plan drafted 2026-07-17 from this session's grounding: three parallel repo explorers (current
auth/household resolution; the policy/settings the engine reads; the Plaid-link identity coupling) and a
web scan of the 2026 fintech auth landscape (Stytch/Clerk/Auth0/Cognito, the authN-vs-KYC split). Four
forks resolved by the user: provider = Stytch; membership join now; Link UI deferred; attestation in
scope.*

*Reviewed 2026-07-18 via `ce-doc-review` (seven personas: coherence, feasibility, security, adversarial,
scope-guardian, product, design). 3 mechanical fixes auto-applied; 16 findings applied via best-judgment
(the U6 split into U6a/U6b; the `/assistant/message` IDOR; the `policy_events` backfill; the live-assembly
dependency named; session revocation/JWKS eviction; the `users` PII guard; mobile token storage; the
zero-membership empty state; the write-screen UX states). Five judgment calls resolved by the user: the
**public demo plane** (KTD-10, replacing the shared key with a read-only `viewer` over `is_demo`
households — this also gave the `role` column its v1 consumer); Link UI stays deferred; Stytch-secret KMS
trigger moved to first real signup; guardrail-loosening accepted in shadow with an audit flag + a
live-mode boot guard. Remaining FYI items (card-fingerprint canonicalization + removal test; `users`
retention/deletion; U2/U7 sandbox-test overlap; JIT concurrent-first-login race; write-endpoint rate
limiting; two-pool-connections-per-request) are logged for implementation, not blocking.*
