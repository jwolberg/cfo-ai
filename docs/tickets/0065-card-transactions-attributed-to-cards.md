---
id: "0065"
title: A linked household's card transactions are never attributed to its cards — so behavior stays UNKNOWN and it can never sweep
type: fix
status: in-progress
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
created: 2026-07-23
refs:
  - pr: 95
---

# Card transactions are never attributed to their cards

Spawned from operating the console against Neon (2026-07-23) and reading `hh_demo_plaid` — the one
real Plaid-linked household — through the live path. Its `/live-decision` returns **REFUSE ·
`card_behavior_unknown`**, and it will return that **every day, forever**, no matter what the
household does. The engine is behaving correctly; the adapter above it is starving it.

## The defect

`build_linked_history` reads **only the checking account's** movements
(`backend/linkedpath.py:88–96`: `r["plaid_account_id"] == checking_id`). The credit-card accounts'
own transactions are dropped on the floor.

Measured on `hh_demo_plaid` (48 ingested transactions across 5 accounts):

| account | txns | sum |
|---|---|---|
| Plaid Checking | 18 | −$1,147.62 |
| **Plaid Credit Card** | **18** | **$12,471.00** ← never attributed |
| Plaid Saving / CD / Money Market | 12 | (not read) |

So the derived portfolio carries, for both cards:

```
behavior = UNKNOWN
observed_monthly_charges = 0.00
observed_monthly_payment = None
```

`derive_portfolio` cannot learn a payment behavior from zero observed card activity, so coverage is
`COMPLETE` in name but every card is `behavior=UNKNOWN`. `CARD_BEHAVIOR_UNKNOWN` is a **blocking
precondition** (`engine/decide.py`) — the engine stops before it forecasts: `projected_low_balance`
is `None`, there is no target, no surplus, no sweep. **A linked household is structurally incapable
of a non-refusal.** The demo's target integration point produces one outcome, and it is the
uninformative one.

## Why it is the blocker

The other three linked-path tickets (`0066` liabilities, `0067` income, `0068` balances) each make
the *number* on the screen more honest. This one is the difference between a decision and a refusal:
until card charges and payments are attributed, the engine has nothing to say yes about, and fixing
the other three changes nothing a user would see.

## The fix

Attribute each card account's transactions to that card, so `derive_portfolio` can observe a real
charge/payment history and resolve `behavior` (revolver / transactor) the same way the seeded
archetypes do:

- Widen `build_linked_history` to read card-account movements, not just checking, and thread them
  into the per-card history `derive_card` / `derive_portfolio` consume (`observed_monthly_charges`,
  `observed_monthly_payment`, and the behavior classification that follows).
- Keep the checking stream as-is for the cash forecast — this adds a second source, it does not move
  the funding account.
- The Plaid Sandbox emits ~18 card transactions over the window, enough to cross the engine's
  "enough cycles to classify" threshold; verify against that data, not a fixture that flatters it.

The honest v1 outcome is still likely a **refuse** on some real ground (thin cash, cadence) — but it
will be a *forecasted* refuse the trace can render, not a blocking one that shows nothing.

## Acceptance criteria

- [x] `build_linked_history` attributes card-account transactions to the matching card; a linked
      household with the Sandbox `user_good` dataset derives at least one card with `behavior !=
      UNKNOWN`. Verified against the **measured** `user_good` shape (see Notes) —
      `tests/test_linkedpath.py::TestCardAccountActivityUnblocksTheDecision`.
- [ ] `hh_demo_plaid`'s `/live-decision` no longer refuses on `card_behavior_unknown`; whatever it
      returns carries a non-null `projected_low_balance` (i.e. the engine forecasted). **Mechanism
      proven in the test; the live confirmation is an operator step against Neon.** Unlike `0066`
      this needs *no* re-sync — the card-account rows are already ingested (the ticket measured 18 of
      them); the read is simply widened to include them, so re-running the decision confirms it.
- [x] A test pins that a linked household with observed card charges + payments produces a
      forecasted decision, not a blocking refusal — the tripwire so this cannot silently regress.
      Proven red-without-fix / green-with-fix (2026-08-15).
- [x] No change to seeded-archetype decisions (the shared `derive_portfolio` path is unchanged for
      them) — the full suite stays green (531/1, exit 0). The linked path is a separate assembly; the
      seeded archetypes never touch `build_linked_history`.

## Notes

- Related but distinct from **`0066`**: liabilities gives the card's *terms* (APR, statement,
  minimum); this ticket gives the card's *observed behavior*. Both feed the same portfolio, and a
  household needs both to earn a real sweep — but only this one is load-bearing for "can it decide
  at all".
- `linkedpath.py`'s module docstring already flags card-charge reconstruction as a deferred
  *normalize* step ("Unbilled card charges are not reconstructed"). This ticket is narrower than
  full normalize: it needs observed history to classify behavior, not an exact-cycle rebuild.
- Surfaced alongside the 2026-07-23 prod re-seed; see `docs/implementation-notes.md` (that day) and
  the [[cfo-ai-link-flow-dogfood]] note.
- **The `user_good` card shape, measured 2026-08-15** (probed the Plaid *Sandbox* API directly, no
  prod): on the credit-card account a purchase **and** the payment are both Plaid-**positive** — the
  "AUTOMATIC PAYMENT - THANK YOU" is +2078.5, same sign as the charges. So charge-vs-payment is told
  apart by **name, not sign** (`_is_card_account_payment`). The pattern is 6 txns/cycle summing
  $4,157; the ticket's measured "18 txns / $12,471" is exactly **3× that** — three cycles, three
  payments, right at the classifier's three-cycle floor. The card payment is *not* funded from the
  linked checking (it lands on the card ledger; a smaller "CREDIT CARD … PAYMENT" comes from
  *savings*), which is why a card payment recorded on the card ledger is treated as a checking
  outflow only as a documented approximation (#3 in `backend/linkedpath.py`), harmless to the
  forward forecast.
