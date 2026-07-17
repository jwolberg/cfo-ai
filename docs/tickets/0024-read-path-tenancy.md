---
id: "0024"
title: The read path — serve from Postgres, scoped by household
type: feature
status: done
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
GET  /households/{id}/decisions/{day}/explain
POST /assistant/message                   -> takes a household_id (in the body)
GET  /health                              -> unchanged, unauthenticated

GET  /spend                               -> UNMOVED. ticket 0031, see below.
```

> **`/spend` was split out into [`0031`](0031-the-spend-surface-per-household.md).** It has two
> unsolved problems and this ticket was a tenancy migration, so it got the same treatment `0030`
> got out of `0023`. (1) Its figures — every overlapping 30-day total by channel, what the card
> took last cycle — come from the whole transaction `History`, and **there is no `transactions`
> table** to rebuild them from until ingest lands ([3.1], unbuilt). (2) `derive_spend_snapshot`
> reads `portfolio.cards[0]`, so on the three portfolio households `0023` seeded it reports an
> arbitrary card as "your card" — the exact bug `0027` and `0030` removed from the walk and the
> artifact. Shipping it here meant either storing a `cards[0]` surface into Postgres, or rushing a
> shape decision inside a migration. It stays honest about its scope instead.

**`Artifact`/`DayRecord`/`Summary`/`SpendSnapshot` survive as response shapes.** They are built from
rows instead of from a file. This is a migration of the *source*, not a rewrite of the API — the
mobile client's `types.ts` should barely move except for the household dimension.

## The artifact stops being persistence and becomes a fixture

`backend/data/decisions.json` is **no longer read by any household route**. It stays in the tree as
the golden file proving archetype A's decisions did not move (`0019`, `0023`, `0030`).

**One exception, and it is named rather than hidden: `/spend` still reads it** — ticket `0031`.
There is one system of record for households and one route that has not moved to it. Saying "the
file is no longer read at runtime" while a route reads it would be exactly the kind of sentence
this repo keeps finding in its own documents.

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

- [x] Every non-health route requires a household and is scoped through `0021`'s repository.
      (`/spend` excepted and split — `0031`.)
- [x] **The demo household's `/decisions` response is identical to the committed artifact's**,
      day for day, amount for amount, reason for reason — plus the summary.
      `TestTheReadPathServesWhatTheFileDid`. **The third place archetype A is the oracle:** `0019`
      proved the walk did not drift, `0023` proved the seeder wrote what the walk decided, and this
      proves the read path serves what the seeder wrote. The chain from `sim/` to the wire is
      pinned end to end.
- [x] A test asserts `main.py` does not read `backend/data/decisions.json` — **narrowed, and the
      narrowing is the honest part.** The feed and the explanation are proved not to touch it by
      *taking it away* (`app.state.artifact = None`) and watching them work. `/spend` still reads
      it, and a test asserts *that* too, so the exception cannot quietly become permanent: it fails
      the day `0031` lands, which is the day to delete it.
- [x] `GET /households` lists the four archetypes with labels a human can pick between — and
      carries **nothing an id alone should not buy**: no balances, no decisions, no counts.
- [x] Startup fails fast if the database is unreachable or unmigrated. **And a third case the
      ticket did not ask for and should have:** a role that bypasses RLS. Reachable and migrated
      is not the same as scoped — Neon's default role is reachable, migrated, and returns every
      household. `assert_rls_binds()` now runs at startup, and `TestStartup` asserts all three.
- [x] Requesting an unknown household is a 404, not a 500 or an empty 200 — and a test asserts it
      is not an empty 200 specifically, because `decisions: []` says the household exists and the
      engine decided nothing for it, which is a different claim and a false one.
- [x] `USERS.md` updated. The switcher is a reviewer's instrument, not a customer feature, and the
      shared-key posture is stated in prose — including the half that is uncomfortable: **an IDOR
      suite is reassuring in a way a shared key does not earn.** `0021` asked for that paragraph;
      this is where it got written.
- [x] `pytest` and `ruff` clean.

## Out of scope

Clerk. Real per-user auth. A signup flow. All land with Plaid, when there is a real user.
