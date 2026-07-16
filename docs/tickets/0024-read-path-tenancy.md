---
id: "0024"
title: The read path — serve from Postgres, scoped by household
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0021", "0023"]
created: 2026-07-16
---

# The read path — serve from Postgres, scoped by household

Implements **U6** of the plan. Single owner: `backend-python-agent`.

**Depends on:** `0021` (repository), `0023` (seeded data to serve).

**Files:** `backend/main.py`, `backend/auth.py`, `backend/artifact.py`, `tests/test_main.py`,
`USERS.md`

## What changes

`app.state.artifact` (`backend/main.py:63`) and `ArtifactDep` (`85-92`) are gone. Routes become
household-scoped:

```
GET  /households                          -> [{id, archetype, label}]
GET  /households/{id}/decisions           -> was GET /decisions
GET  /households/{id}/spend               -> was GET /spend
GET  /households/{id}/decisions/{day}/explain
POST /assistant/message                   -> takes a household_id
GET  /health                              -> unchanged, unauthenticated
```

**`Artifact`/`DayRecord`/`Summary`/`SpendSnapshot` survive as response shapes.** They are built from
rows instead of from a file. This is a migration of the *source*, not a rewrite of the API — the
mobile client's `types.ts` should barely move except for the household dimension.

## The artifact stops being persistence and becomes a fixture

`backend/data/decisions.json` is **no longer read at runtime**. It stays in the tree as the golden
file proving archetype A's decisions did not move (`0019`, `0023`).

This is not ADR-0002 being "extended" — nothing reads it at runtime, there is one system of record,
and the file is a test input like any other. ADR-0004 [3] says so explicitly.

## ⚠️ The auth gap must be flagged in `USERS.md`, not just in a comment

Auth stays the shared API key plus **explicit household selection**. Any key may select any
household. Seeded synthetic households have no owner to authenticate as, and Clerk lands with Plaid.

**The mechanism (`0021`) is real and tested. The identity is not.** `backend/auth.py`'s own docstring
already says the key is *"a lock on a door, not an identity system"* — that stays true and now
matters more, because an IDOR suite is reassuring in a way a shared key does not earn.

`USERS.md` §1 currently says the customer "cannot ... manage more than one household." That line is
now wrong in a specific way worth correcting rather than deleting: **the demo can switch households
because the demo is not a customer.** Say that.

## Acceptance criteria

- [ ] Every non-health route requires a household and is scoped through `0021`'s repository.
- [ ] **The demo household's `/decisions` response is byte-identical to today's**, modulo the route
      change. This is the third place archetype A is the oracle.
- [ ] A test asserts `main.py` does **not** read `backend/data/decisions.json` at runtime.
- [ ] `GET /households` lists the four archetypes with labels a human can pick between.
- [ ] Startup fails fast if the database is unreachable or unmigrated — ADR-0004 [3.2]: a service
      holding bad data must refuse rather than serve wrong numbers one request at a time.
- [ ] Requesting an unknown household is a 404, not a 500 or an empty 200.
- [ ] `USERS.md` updated: the switcher exists, and the shared-key/any-household posture is stated
      plainly as a demo posture.
- [ ] `pytest` and `ruff` clean.

## Out of scope

Clerk. Real per-user auth. A signup flow. All land with Plaid, when there is a real user.
