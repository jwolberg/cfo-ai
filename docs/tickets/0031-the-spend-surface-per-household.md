---
id: "0031"
title: The spend surface, per household — blocked on ingest, and that is the finding
type: feature
status: blocked
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0024-read-path-tenancy.md
depends_on: ["0023", "0024", "0030"]
blocked_by: "ingest — architecture.md [3.1], not built"
created: 2026-07-17
---

# The spend surface, per household — blocked on ingest, and that is the finding

**Decided 2026-07-17: this waits for the `transactions` table, and does not fake one.**

The ticket's own Out of Scope said *"if this ticket concludes that the surface should wait for them,
that is a finding worth writing down rather than a reason to store a projection quietly."* It does,
and this is it.

## What was decided, and against what

`/spend` needs `rolling_30d_cash` / `rolling_30d_card` (every overlapping 30-day total by channel)
and `charged_last_cycle` / `paid_last_cycle`. All four are derived from the full transaction
`History`, and there is no `transactions` table — that is ingest, [3.1], not built.

Three options. The two rejected ones are why this is a decision and not a delay:

**Store the derived surface** — the seeder holds the `History`, so it could compute `SpendSnapshot`
and store it per household. Small, and it would have closed the ticket today: the artifact already
carries a `spend: SpendSnapshot` field, so this is that field relocated rather than a new concept.
**Rejected because it stores an answer instead of the data the answer comes from.** `/spend` would
be a lookup of something precomputed at seed time, and every question a reviewer might ask of it
("what about a different window?") would need another seeder run. Ingest deletes the table anyway.

**Build the `transactions` table now** — seed `sim`'s `Txn`s into it and derive `/spend` from rows,
the way production eventually will. **Rejected, and this is the sharper one.** [4] designs that
table *with* `pending_transaction_id`, a `reconciled_with` link, and an `internal_transfer_pair`
link. [3.2] calls pending→posted reconciliation and internal-transfer detection **"the two hard
problems, each of which corrupts the forecast silently if wrong."** `sim`'s `Txn` is
`(day, amount, label, kind, card_id)` and has none of them.

A table with the designed name and none of the hard parts is not a head start on [3.1]; it is a
thing that **looks** like the designed table and is not one — and ingest would then have to
reconcile with the fake. That is this repo's signature failure, and it would have been introduced
deliberately, in the schema, to make one route look finished.

## What shipped instead

**The landmine `0030` armed, disarmed.** `0027` refused a multi-card spec at `build()` because
`DayRecord` held one debt — and that refusal was also, quietly, protecting
`derive_spend_snapshot`'s `cards[0]`. `0030` fixed `DayRecord` and lifted the guard, and
`build(archetype_b)` then produced an artifact whose `debts` listed three cards correctly and whose
`spend` surface silently described `card_b_high` alone. Measured, not theorised.

So the refusal moved down to the field that is actually still single-card, and says what it
actually is. `build()` still serves the demo — the only thing it is for; the seeder walks
portfolios into Postgres through `walk()` and never comes here.

**The Spending tab already tells the truth** (`0025`): for any household but the demo's it says so,
rather than rendering the demo's figures under another household's label.

## The consequence, stated rather than buried

**`backend/data/decisions.json` is read at runtime, indefinitely.** ADR-0004's one-system-of-record
is not true yet and will not be until ingest lands, which is Plaid — the largest unbuilt thing in
this repo. "Temporary" here means *years*, plausibly, and the honest word for that is not temporary.

That is the price of the decision, and it is worth paying: a fake `transactions` table would make
[3.1] harder than not having one, and a stored projection would make `/spend` a cache of an answer
nobody can re-ask. Neither buys anything a reviewer actually wants — the decision feed, which is
what "understand the dashboards" meant, works for all four households today.

## When this unblocks

[3.1] ingest, or the moment anything real needs a spend surface for a household that is not the
demo's. Then:

- [ ] `GET /households/{id}/spend`, scoped through `0021`'s repository like every other route.
- [ ] It describes **every** card, not `cards[0]`. The reserve (`held_back`) stays portfolio-level
      — `untouchable()` reserves against every card at once — while the statement, the unbilled
      balance and the cycle's charges are per card. Deciding which parts are which is most of the
      work, and the argument in `SpendSnapshot`'s docstring does not weaken with three cards: "the
      two obligations are kept separate because they are due a month apart, and a single 'what you
      owe' number hides exactly the thing the user needs to see."
- [ ] `derive_spend_snapshot`'s guard comes out, replaced by the shape rather than deleted.
- [ ] `main.py` no longer reads `backend/data/decisions.json` **at all**, and
      `TestTheFileIsNoLongerTheSource::test_spend_is_the_one_that_still_does_and_says_so` is
      **deleted** rather than updated — it exists to fail on that day.
- [ ] `app.state.artifact` is gone. The file stays as the golden fixture proving archetype A's
      decisions never moved (`0019`, `0023`, `0030`) — a test input, not persistence. ADR-0004 [3]
      already says so.
- [ ] `mobile/src/screens/Spending.tsx`'s "we can't show this yet" state is deleted, and the tab
      follows the switch for real — closing `0025`'s one `[~]`.
