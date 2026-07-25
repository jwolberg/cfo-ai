---
id: "0068"
title: A linked household's non-checking balances are invisible, and its savings is a hardcoded $2,400 constant
type: fix
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
created: 2026-07-23
---

# A linked household's savings is a demo constant, not its real balance

Spawned from operating the console against Neon (2026-07-23). The snapshot the engine decides on for
`hh_demo_plaid` reports a savings balance of **$2,400.00** — a number that appears in **none** of the
household's twelve Plaid accounts. It is a hardcoded seed constant, reused verbatim for a real
linked household.

## The defect

`assemble_snapshot` builds a two-account snapshot — checking (real, passed in) and a savings account
whose balance is the module constant `SAVINGS_BALANCE = money("2400.00")`
(`backend/precompute.py:170`, used at `:881–884`). This is fine for the seeded archetypes, which are
synthetic and have no real savings. But the **linked** path reuses the same `assemble_snapshot`, so
a real household's savings is overwritten with the demo's $2,400 and every other depository balance
is dropped.

Measured on `hh_demo_plaid` — real Plaid balances vs what the engine sees:

| account | real balance | in the snapshot? |
|---|---|---|
| Checking | $110.00 | yes (correct) |
| Savings | $210.00 | replaced with **$2,400.00** |
| Money Market | $43,200.00 | invisible |
| Cash Management | $12,060.00 | invisible |
| HSA | $6,009.00 | invisible |
| CD | $1,000.00 | invisible |

Roughly **$62k** of real, healthy balances are either wrong or unseen. `IDLE_CASH_ELSEWHERE` — the
reason code whose whole job is to notice money sitting in savings — is reasoning about a fabricated
figure.

## The fix

For a **linked** household, read the savings (and other relevant depository) balance from the
ingested `plaid_accounts` rather than the constant. Options, in preference order:

1. Thread the real savings balance from `build_linked_history` / `livepath` into `assemble_snapshot`
   so the linked path passes its own figure, exactly as it already passes the real checking balance
   — leaving `SAVINGS_BALANCE` as the seeded-only default.
2. If the engine's account model should see more than one savings bucket, decide how money market /
   cash management map onto the snapshot's account kinds (they are all "idle cash elsewhere" to the
   `IDLE_CASH_ELSEWHERE` rung) — but keep that a deliberate modeling choice, not an accident.

Whatever the shape: **the linked household's savings must not be a constant**, and the seeded
archetypes must keep their existing behavior (they have no real balance to read).

## Acceptance criteria

- [ ] A linked household's snapshot savings balance is derived from its ingested Plaid balances, not
      `SAVINGS_BALANCE`.
- [ ] `hh_demo_plaid`'s snapshot reflects a savings figure that exists in its Plaid data (and a
      documented decision on whether money-market / cash-management balances are folded into "idle
      cash").
- [ ] Seeded-archetype snapshots are byte-for-byte unchanged (they still take the `SAVINGS_BALANCE`
      default) — the schema/engine suites stay green.

## Notes

- Lowest-severity of the linked-path set for the *decision* (`IDLE_CASH_ELSEWHERE` is a
  non-blocking, informational rung), but the highest-severity for *credibility*: a CEO looking at
  the raw account facts will see $62k of real balances and a snapshot that invents $2,400. It reads
  as "the integration doesn't work" even when the decision is defensible.
- Depends on nothing else in the set, but is most naturally done alongside **`0065`**, which already
  widens `build_linked_history` to read more than the checking account.
