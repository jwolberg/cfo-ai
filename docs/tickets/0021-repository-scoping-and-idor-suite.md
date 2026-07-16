---
id: "0021"
title: Repository scoping and the IDOR suite
type: feature
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0020"]
created: 2026-07-16
---

# Repository scoping and the IDOR suite

Implements **U3** of the plan. Single owner: `backend-python-agent`.

**This is the security unit.** [`architecture.md`](../architecture.md) [4]: *"An IDOR here exposes
someone's complete financial life; one forgotten `WHERE` clause is not an acceptable single point of
failure."* §7.1 names the pattern: repository-layer scoping **and** Postgres RLS, **gated by an IDOR
test suite.**

**Depends on:** `0020` (schema, RLS policies).

**Files:** `backend/db/repository.py`, `backend/db/session.py`, `tests/test_idor.py`

## What to build

A repository layer where **every** query is scoped by `household_id`, over a session that sets the
RLS variable per transaction. Two independent layers, and the suite proves each **without the
other** — defense in depth that is only tested end-to-end is one layer wearing a disguise.

## ⚠️ `SET LOCAL`, never `SET`

**Neon pools connections. This is the trap, and it would pass a naive IDOR suite.**

```python
# WRONG — leaks the tenant to whoever gets this connection next
conn.execute(text("SET app.household_id = :h"), {"h": hid})

# RIGHT — transaction-scoped, released with the transaction
with conn.begin():
    conn.execute(text("SET LOCAL app.household_id = :h"), {"h": hid})
```

A plain `SET` persists for the life of the *connection*, not the transaction. Under pgbouncer the
next checkout inherits it — **an IDOR wearing a security feature's clothes.** A test suite that
opens one connection and reuses it will never see this, which is exactly why the acceptance criteria
below require the leak test.

## Acceptance criteria

- [ ] Every repository method takes a household scope; none can be called without one. Prefer a
      constructor-scoped repository over a per-call argument someone can forget.
- [ ] **The IDOR suite covers every household-scoped table**, not a sample: for each of `accounts`,
      `cards`, `policies`, `decisions`, `snapshots`, a repository scoped to A cannot **read**,
      **update**, or **delete** a row belonging to B.
- [ ] **Each layer proven alone.** RLS asserted with the repository bypassed (raw SQL, application
      role). Repository scoping asserted independently. If either is removed, tests go red — verify
      by deleting each and watching it fail.
- [ ] **The leak test:** a connection returned to the pool and checked out again does **not** carry
      the previous `app.household_id`. Assert the variable is unset/empty, not merely different.
- [ ] A query with no scope set returns **zero rows** — RLS fails closed, matching
      [`architecture.md`](../architecture.md) [1.1]'s second principle.
- [ ] `pytest` and `ruff` clean.

## Read this before claiming the suite passes

The identity feeding this is **not real** — `0024` keeps the shared API key, and any key may select
any household. **The mechanism is real and tested; the identity is not.** An IDOR suite is
reassuring in a way that a shared key does not earn, and the gap must be flagged in `USERS.md`
(`0024`), not only in a code comment.

That is a deliberate scope boundary: seeded synthetic households have no owner to authenticate as,
and Clerk lands with Plaid. It is not an excuse to weaken this ticket — the suite is what makes the
mechanism trustworthy the day a real identity arrives.
