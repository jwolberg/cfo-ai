---
id: "0031"
title: The spend surface, per household — and for a portfolio
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0024-read-path-tenancy.md
depends_on: ["0023", "0024", "0030"]
created: 2026-07-17
---

# The spend surface, per household — and for a portfolio

**Split out of `0024`, which migrated every other route to Postgres.** `/spend` is the one route
still reading `backend/data/decisions.json`, and the one route still serving a single household. It
is the whole of what stands between this service and having one system of record.

It was split rather than rushed because it needs **two** decisions, and `0024` was a tenancy
migration — the same reason `DayRecord`'s portfolio became `0030` rather than riding along inside
`0023`.

**Files:** `backend/main.py`, `backend/precompute.py` (`derive_spend_snapshot`),
`backend/seed.py`, `backend/readpath.py`, `mobile/src/screens/Spending.tsx`, tests.

## Problem 1 — the data is not in the database

`SpendSnapshot` carries `rolling_30d_cash` and `rolling_30d_card` (every overlapping 30-day total
in the trailing window, by channel) and `charged_last_cycle` / `paid_last_cycle`. All four are
derived from the full transaction `History`.

**There is no `transactions` table.** That is ingest — `architecture.md` [3.1] — and it is not
built. The `decisions` and `snapshots` rows do not carry it either: a `Snapshot` holds
`spend_30d_high` and `daily_discretionary_high`, which are the forecast's inputs, not the series.

So either the seeder stores the derived surface (a projection: honest, and something ingest would
later delete), or this waits for the transactions table it actually wants. **The second is
architecturally cleaner and blocks the switcher's Spending tab for three of four households in the
meantime.** That is the trade to make deliberately.

## Problem 2 — it reads `cards[0]`

`derive_spend_snapshot` (`backend/precompute.py`) is `card = portfolio.cards[0] if portfolio.cards
else None`, and everything it reports — the closed statement, the unbilled balance, what the card
took last cycle against what came off it — describes **that one card**.

For the three portfolio households `0023` seeded, that is an arbitrary card presented as "your
card". It is the same defect `0027` fixed in the walk and `0030` fixed in the artifact, in its last
remaining home. **Do not fix problem 1 without fixing this**, or the fix stores a `cards[0]`
surface into Postgres and the bug outlives all three tickets that removed it.

It is invisible today only because `build()` refused portfolios until `0030`, and because this
route still serves the one household that has never had more than one card.

## The shape question, and it is a real one

The Spending screen renders "this cycle: statement / unbilled / held back" and "last cycle:
charged / paid / grew by". For a portfolio, each of those is per card — but the **reserve**
(`held_back`) is a portfolio-level fact, because `untouchable()` reserves against every card at
once. So the surface is neither purely per-card nor purely aggregate, and deciding which parts are
which is most of this ticket.

Worth remembering while deciding: the two obligations are reported separately because they fall due
a **month apart**, and "a single 'what you owe' figure hides exactly the thing the user needs to
see." That argument does not stop applying because there are three cards. It gets stronger.

## Acceptance criteria

- [ ] `GET /households/{id}/spend`, scoped through `0021`'s repository like every other route.
- [ ] It describes **every** card, not `cards[0]`. A portfolio household's response names all of
      them.
- [ ] The demo household's `/spend` response is unchanged, modulo the route. Archetype A is the
      oracle here as everywhere else.
- [ ] `main.py` no longer reads `backend/data/decisions.json` **at all**, and
      `TestTheFileIsNoLongerTheSource::test_spend_is_the_one_that_still_does_and_says_so` is
      deleted rather than updated — it exists to fail on the day this lands.
- [ ] `app.state.artifact` is gone. The file stays in the tree as the golden fixture proving
      archetype A's decisions never moved (`0019`, `0023`, `0030`), which is a test input, not
      persistence. ADR-0004 [3] already says this.
- [ ] `mobile/src/screens/Spending.tsx` renders whatever shape this lands on.
- [ ] `pytest` and `ruff` clean.

## Out of scope

The `transactions` table and ingest ([3.1]). If this ticket concludes that the surface should wait
for them, **that is a finding worth writing down** rather than a reason to store a projection
quietly.
