---
id: 0005
title: The webhook doorbell's two tenancy exceptions — an unscoped raw table and a definer lookup
anchor: ADR-0005
status: accepted
date: 2026-07-17
supersedes:
superseded-by:
---

## [1] Context

[ADR-0004](./0004-postgres-scoped-by-household.md) made one rule not negotiable: **every table is
scoped by `household_id`, enforced at the repository layer and by Postgres row-level security**, and
[`architecture.md`](../architecture.md) [4] calls a forgotten `WHERE` clause "not an acceptable
single point of failure." The Plaid transport rung
([`../plans/2026-07-17-001-feat-plaid-transport-rung-plan.md`](../plans/2026-07-17-001-feat-plaid-transport-rung-plan.md))
opens the first two holes in that rule, and this ADR is where they are argued and bounded so they do
not become three.

The forcing fact is Plaid's: **a webhook names an `item_id`, not a `household_id`.** The doorbell
receives `{item_id, webhook_type, webhook_code, ...}` from a public endpoint and must persist it and
hand off a sync — but it cannot know whose household this is until it reads `plaid_items`, and
`plaid_items` is FORCE'd, so an unscoped read returns nothing (RLS fails closed, by design). Two
things therefore cannot both be true: "the raw store is scoped" and "the doorbell can write it".

## [2] Decision

Two narrow, named exceptions — and no others.

### [2.1] `plaid_webhooks` is unscoped

The raw webhook store carries no `household_id` and is deliberately **outside `HOUSEHOLD_SCOPED`**,
with no RLS policy. It mirrors `GET /households` (`backend/main.py`), the one existing read that is
not household-scoped because it is *about* the set of households rather than one household's data.

Two alternatives were rejected:

- **A scoped raw table with a household resolved at write time.** The doorbell would have to resolve
  `item_id → household_id` before persisting — which is the very read RLS forbids without a scope,
  and doing it inline is exactly the slow handler `architecture.md` [3.1] says causes the retries
  that cause duplicates. It also means losing the raw payload if resolution fails, which defeats the
  point of a raw store (an audit record of what Plaid actually sent).
- **No raw table; resolve and sync inline.** Same inline-work problem, and nothing to replay a missed
  or malformed delivery against.

The dedup key is `UNIQUE NULLS NOT DISTINCT (plaid_item_id, webhook_code, cursor)`. `cursor` is NULL
for the webhook that matters most (TRANSACTIONS / SYNC_UPDATES_AVAILABLE carries none), and under
Postgres's default NULL-distinct semantics two such redeliveries would both be admitted — the
opposite of dedup. `NULLS NOT DISTINCT` (Postgres 15+, and both CI and Neon are ≥15) treats them as
equal, so `ON CONFLICT DO NOTHING` drops the redelivery. This is a best-effort guard on the
*enqueue*; the sync is idempotent by cursor regardless (a redelivered sync resumes from the stored
cursor and returns an empty page).

### [2.2] `plaid_household_for_item(text) RETURNS text`, SECURITY DEFINER

The worker still needs the mapping the doorbell could not resolve. It reads it through one
`SECURITY DEFINER` SQL function that runs as its owner — the migration-runner, a superuser or
BYPASSRLS role in every environment that runs migrations — and therefore bypasses `plaid_items`'
FORCE'd RLS, returning a single `household_id` and nothing else. The worker then `SET LOCAL
app.household_id` and every financial read from that point is scoped normally.

The alternative — **granting `cfo_app` the `BYPASSRLS` attribute** — was rejected outright: it would
unscope *every* query the application makes, collapsing `architecture.md` [4]'s two layers to zero,
with no symptom and a green IDOR suite. `assert_rls_binds()` exists precisely to refuse a role with
that attribute at startup. The definer function is the opposite: RLS is bypassed for exactly one
lookup, on one column, through one audited surface with `EXECUTE` granted narrowly and
`search_path` pinned so the body cannot be hijacked. (`households.id` is `text` in this schema, so
the function returns `text`, not the `uuid` the plan sketched.)

Both exceptions are proven, not asserted: `tests/test_idor.py` shows an unscoped `cfo_app` session
reads **zero** rows from `plaid_items` directly and yet resolves the household through the function —
the property that makes the worker executable at all, and the P0 the plan's own review caught.

## [3] Consequences

- **Retention.** An unscoped table that accumulates third-party payloads is an unscoped *archive* if
  nothing prunes it. The policy: `plaid_webhooks` rows are operational, not a system of record —
  purge rows older than **N days** (default 30) on the same Cloud Scheduler cadence as the nightly
  reconciliation poll (U4). The raw store exists to debug and replay recent deliveries, not to keep
  them forever. The grant includes `DELETE` for exactly this; the purge job itself lands with the
  scheduler in U4.
- **These are the only two exceptions.** Any future unscoped table or definer function is a new ADR,
  not a precedent set here. The IDOR suite remains the gate: every table in `HOUSEHOLD_SCOPED` is
  proven isolated with the repository bypassed, and `plaid_webhooks`' absence from that tuple is a
  deliberate, reviewed line, not an omission.
- **The doorbell is a second unauthenticated-by-our-key endpoint** (the first being `/health`). Its
  authentication is Plaid's `Plaid-Verification` signature, verified before any persist or enqueue;
  the Cloud Tasks push target additionally carries an OIDC token so only the queue can invoke the
  worker route. Neither is our API key, and that is correct: Plaid does not have it.

## [4] Provisioning this depends on (named because nothing checks it at startup)

Unlike `assert_rls_binds`/`_assert_migrated`, a missing queue or an ungranted service account fails
at the *first webhook*, not at deploy. So they are named here and in `backend/plaid/tasks.py`:

1. Create the Cloud Tasks queue (`PLAID_TASKS_QUEUE` in `PLAID_TASKS_LOCATION`).
2. Grant the service account `cloudtasks.enqueuer`.
3. Require an **OIDC token** on the push target so only Cloud Tasks can invoke the worker route.
4. (U4) A Cloud Scheduler job for the nightly reconciliation poll and the retention purge.
