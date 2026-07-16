---
id: "0027"
title: A ledger per card — the walk cannot simulate a portfolio
type: bug
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: []
created: 2026-07-16
---

# A ledger per card — the walk cannot simulate a portfolio

**Blocks `0023`'s archetypes B and C.** Found by trying to build them; not in that ticket.

**Depends on:** nothing. `0019` (the unified walk) is done and is what makes this a single fix
rather than three.

**Files:** `backend/precompute.py`, `tests/test_precompute.py`

## The defect

A two-card household with a **$14,000** card and a **$3,000** card reports
`statement_balance=14009.20` for **both**:

```
spec says:      card_big=$14,000.00   card_small=$3,000.00
  snapshot: card_big     statement=14009.20  unbilled=0.00
  snapshot: card_small   statement=14009.20  unbilled=0.00
```

`walk()` builds **one** `DebtLedger` from `spec.card` (`backend/precompute.py:909`), and
`assemble_snapshot()` hands that single `ledger_balance` to `derive_card()` for **every** card.

> `HouseholdSpec.card`'s own docstring (`sim/household.py:243-250`): *"Deliberately **not** a
> compatibility shim for the reserve: anything that reserves, ranks or forecasts must iterate
> `cards`, because reading `.card` on a two-card household is exactly the bug this feature exists
> to fix."*
>
> The walk reads `.card`.

**What it costs downstream:** `_select_target` (`engine/decide.py:172-206`) ranks both cards at the
same balance and picks on APR alone. The obligation reserve — the safety fix `0013` exists for —
counts $14,009 twice. Neither is a rounding error; both are the engine reasoning about a household
that does not exist.

**Why it survived.** The engine's multi-card types, the portfolio reserve and the ranking
(`0010`–`0017`) are built and tested. But **every household the walk has ever driven has one card**,
so nothing exercised them. Same shape as every other defect this plan has surfaced: a mechanism
built, tested, and never actually exercised. No symptom. Green tests.

## Four single-card assumptions, not one

1. **`walk()`'s ledger** — `DebtLedger(principal=spec.card.balance, apr=spec.card.apr)`
   (`precompute.py:909`). One ledger, whichever card happens to be first.
2. **`assemble_snapshot()`'s `ledger_balance`** — a scalar, passed to every `derive_card()`.
3. **`card_payments`** — `{t.day: -t.amount for t in history.txns if t.kind is CARD_PAYMENT}`
   (`precompute.py:910`). This discards `Txn.card_id`, **and collides**: two cards paid on the same
   day and one payment silently overwrites the other. A dict keyed on `day` cannot hold a portfolio.
4. **`DebtLedger.accrue()`** — posts when `day.day == STATEMENT_DAY`, the module constant
   (`precompute.py:286`). It ignores the card's own `close_day_of_month`, so two cards with
   different cycles would both post on the 20th.

## What the simulator already gives you

**`Txn.card_id` exists** — `sim/household.py:92`: *"Which card ledger this belongs to. Set on
`CARD_CHARGE` and `CARD_PAYMENT`; None otherwise."* The attribution is already there. The walk
throws it away.

`Decision.target_debt_id` says which card a sweep was aimed at. The walk ignores that too and pays
its one ledger.

## What to build

- **`ledgers: dict[str, DebtLedger]`**, one per `spec.cards`, keyed by `card_id`.
- **`DebtLedger` gains `close_day`**, defaulted to `STATEMENT_DAY` so today's demo is unchanged, and
  posts on its own card's day.
- **Payments keyed by `(day, card_id)`**, read off `Txn.card_id`. Accumulate rather than overwrite.
- **Sweeps carry their target**: `dict[date, tuple[Decimal, str]]`. Settlement pays
  `ledgers[target_debt_id]`, not "the" ledger.
- **`assemble_snapshot(ledger_balances: Mapping[str, Decimal])`** — per card.
- **`WalkDay.debt_balances: Mapping[str, Decimal]`**, replacing the scalar `debt_balance`.

**`build()` stays single-card, and should say so.** `DayRecord` carries one `debt_balance`, one
`debt_apr`, one `debt_id` — the artifact schema has exactly one debt because the demo household has
exactly one card. Passing a multi-card spec to `build()` must **raise**, not silently report
`cards[0]`. That is this ticket's own bug, and shipping the fix while leaving the same trap next
door would be absurd.

## Acceptance criteria

- [ ] **The demo artifact is byte-identical and NOT regenerated.** For a one-card household a
      per-card ledger *is* the single ledger, so `tests/test_precompute.py:489-494` must stay green
      untouched. If it goes red the refactor is wrong — not the file.
- [ ] `calibrate.measure(None)` is unchanged: **4,320 graded days, 2.338% breach, 0 sweep-caused
      overdrafts, $544,640.58 false-refusal cost.** The population is single-card, so nothing here
      may move it.
- [ ] **The failing test first.** Assert a two-card household reports each card's own balance. It
      must fail on today's code with `14009.20 == 14009.20` — verify that before fixing.
- [ ] A sweep settles against the card `Decision.target_debt_id` names, and **only** that card.
- [ ] Two cards paid on the same day both land. (Today one is dropped.)
- [ ] Two cards with different `close_day_of_month` post their interest on their own days.
- [ ] `build()` raises on a spec with more than one card.
- [ ] `pytest` and `ruff` clean.

## Out of scope

Archetypes B/C themselves (`0023`), and archetype D's APR-visibility question, which is a separate
modelling call. This ticket makes the walk *able* to carry a portfolio; it seeds nothing.
