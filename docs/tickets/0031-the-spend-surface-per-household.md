---
id: "0031"
title: The spend surface, per household — and for a portfolio
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0024-read-path-tenancy.md
depends_on: ["0023", "0024", "0030"]
created: 2026-07-17
completed: 2026-07-16
spawned: ["0033"]
supersedes: "PR #50 — status: blocked, 'waits for the transactions table'"
---

# The spend surface, per household — and for a portfolio

> **Done — and this reopens a ticket #50 closed as `blocked`.** That PR decided *"this waits for the
> `transactions` table, and does not fake one"*, and most of its reasoning stands: its two rejected
> options are still rejected here, for its own reasons. What it missed is arithmetic, not judgment —
> **five of `SpendSnapshot`'s nine fields never needed ingest at all.** Read
> [Superseding #50](#superseding-50) before the rest.

## Superseding #50

#50 enumerated the blocked surface as:

> `/spend` needs `rolling_30d_cash` / `rolling_30d_card` … and `charged_last_cycle` /
> `paid_last_cycle`. **All four** are derived from the full transaction `History` […] not built.

True of those four. `SpendSnapshot` has **nine** fields, and the other five —
`statement_balance`, `statement_due`, `unbilled_balance`, `unbilled_due`, `reserved` — come off the
frozen `Snapshot`, which `0022` has stored since it landed. `untouchable()` is a pure function of
that snapshot. So the whole "this cycle" panel was derivable from rows, per card, for every
household, on every request, with no `transactions` table and nothing stored twice.

The four were treated as the surface; they are **less than half of it**. That is the entire
disagreement.

### Where #50 was right, and this agrees

- **"Build the `transactions` table now" is still rejected, for #50's sharper reason.** [4] designs
  it with `pending_transaction_id`, `reconciled_with`, `internal_transfer_pair`; [3.2] calls
  pending→posted reconciliation and internal-transfer detection *"the two hard problems, each of
  which corrupts the forecast silently if wrong."* `sim`'s `Txn` has none of them. A table with the
  designed name and none of the hard parts is not a head start on [3.1] — ingest would have to
  reconcile with the fake. **No `transactions` table is created here.**
- **The `cards[0]` landmine was real and #50 measured it.** `0030` fixed `DayRecord` and silently
  disarmed the guard protecting `derive_spend_snapshot`'s `cards[0]`, so `build(archetype_b)`
  produced correct `debts` and a `spend` surface describing one arbitrary card. #50 put a `raise`
  there, which is right for a single-card surface. This removes the surface instead, so the guard
  has no subject — #50's own unblock AC: *"`derive_spend_snapshot`'s guard comes out, replaced by
  the shape rather than deleted."*

### Where it costs something, stated rather than buried

#50's objection to storing a projection is the real one and it is **not fully answered**:

> a stored projection would make `/spend` a cache of an answer nobody can re-ask.

That lands, on the History-derived half. `spend_projections` holds the rolling series and each
card's last cycle; *"what about a different window?"* needs a re-seed. Two things narrow it — the
obligations are re-derived live per request rather than stored, so the cache is four fields rather
than nine; and ingest deletes the table. It does not remove it.

**What is bought for that cost** is the thing #50 priced and accepted the other way:

> `backend/data/decisions.json` is read at runtime, **indefinitely** […] "Temporary" here means
> *years*, plausibly, and the honest word for that is not temporary.

That ends here. `app.state.artifact` is gone, ADR-0004's one-system-of-record is true for every
route, and `0025`'s Spending tab stops refusing for three of four households. The trade is a
four-field projection ingest deletes, against serving a JSON file for years — **taken deliberately,
by the repo owner, with both options and this finding in front of them.**

Reopened rather than filed anew: a ticket that was closed on a premise that turned out to be
under-scoped should say so in place. Spawned [`0033`](
0033-a-migration-that-imports-live-code-is-not-a-migration.md).

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

- [x] `GET /households/{id}/spend`, scoped through `0021`'s repository like every other route.
- [x] It describes **every** card, not `cards[0]`. A portfolio household's response names all of
      them.
- [x] The demo household's `/spend` response is unchanged, modulo the route. Archetype A is the
      oracle here as everywhere else.
- [x] `main.py` no longer reads `backend/data/decisions.json` **at all**, and
      `TestTheFileIsNoLongerTheSource::test_spend_is_the_one_that_still_does_and_says_so` is
      deleted rather than updated — it exists to fail on the day this lands.
- [x] `app.state.artifact` is gone. The file stays in the tree as the golden fixture proving
      archetype A's decisions never moved (`0019`, `0023`, `0030`), which is a test input, not
      persistence. ADR-0004 [3] already says this.
- [x] `mobile/src/screens/Spending.tsx` renders whatever shape this lands on.
- [x] `pytest` and `ruff` clean.

## Out of scope

The `transactions` table and ingest ([3.1]). If this ticket concludes that the surface should wait
for them, **that is a finding worth writing down** rather than a reason to store a projection
quietly.

---

# How it resolved

## Problem 1 was a false choice. The surface splits.

The ticket offered two options and called the trade a real one. It is narrower than that, because
`untouchable()` is a **pure function of the `Snapshot`** — and `0022` has been storing the whole
frozen snapshot since it landed.

| | source | needs ingest? |
|---|---|---|
| statement, unbilled, `held_back` | the stored `Snapshot` | **no** |
| charged/paid last cycle, the rolling 30-day series | the transaction `History` | **yes** |

So the entire "this cycle" panel was already derivable from rows, for **every** household, per card,
on every request — no projection, no ingest, nothing waiting. Only the History-derived half needed
the decision this ticket was split out to make.

**Resolution: derive the obligations live and never store them; store only the projection**
(`spend_projections`, which ingest deletes). Storing the obligations too would duplicate what
`snapshots.payload` holds and let the two disagree about what the engine saw — `readpath.py`'s own
argument against denormalizing display fields onto `decisions`, and [4.1]'s about not restructuring
storage on a guess. The projection is the *smallest thing that cannot be derived*, and it is written
down rather than stored quietly, which is what "Out of scope" asked for.

## Problem 2, and the shape question that answers itself

`held_back` is **not** an indivisible portfolio-level fact. `untouchable()` is
`sum(obligation_in_horizon(card, horizon_end) for card in cards)`, and a sum decomposes into its
terms: each card's `held_back` is its own `obligation_in_horizon` — an attribution, not an
allocation. So the response is per card **and** carries a portfolio total, both true at once, pinned
together by `test_the_per_card_reserve_sums_to_the_portfolio_reserve` across all three portfolio
archetypes.

There is deliberately **no `totals.due`**. The cards do not close together, so a single due date
would be a fiction — the month-apart argument getting stronger with three cards, exactly as this
ticket predicted it would.

## The near-miss

`cards.observed_monthly_charges` / `observed_monthly_payment` are already columns and look like they
would remove the need for a projection. They are a **different number wearing the right label**:
trailing engine inputs averaged over a window, against `charged_last_cycle`'s exact `[close, close]`
cycle. It would have been wrong and shown no symptom.

## What it spawned

[`0032`](0032-a-migration-that-imports-live-code-is-not-a-migration.md) — `alembic/versions/0001`
imported `HOUSEHOLD_SCOPED` from live application code. Adding a table to that constant retroactively
changed what revision 0001 *does*, and a fresh `alembic upgrade head` would have failed at 0001
while every already-migrated database stayed green. Fixed here because this ticket could not land
without it; filed because the *rule* outlives the fix.

## What it unblocked

**`0025`'s Spending tab closes for real.** It could only refuse for three of four households while
the route was single-tenant. The screen now follows the switch like every other one, and the
refusal panel is deleted.
