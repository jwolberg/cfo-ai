---
id: "0066"
title: The Plaid Link token requests only `transactions` — so every linked card's APR, statement and minimum are estimated, not reported
type: fix
status: open
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
created: 2026-07-23
---

# Request the `liabilities` product at link time

Spawned from operating the console against Neon (2026-07-23). The link token is created with
`products=[Products("transactions")]` (`backend/plaid/link.py:68`) and nothing else. `liabilities`
is never requested, so `/liabilities/get` has nothing to return, `plaid_liabilities` is empty, and
every card term the engine actually targets on is **inferred** instead of **reported**.

This is the quick win of the linked-path set — one product in a list — and it is the term the engine
cares about most.

## The defect

Measured on `hh_demo_plaid`:

- `plaid_liabilities` rows: **0**.
- Both derived cards: `apr = 0.23`, `apr_source = ESTIMATED`, statement balance and minimum payment
  from a fallback rather than the issuer's own figures.

`ingest_account_state` already **degrades to balances-only** when `liabilities` is absent
(`backend/plaid/accounts.py` / `sync.py:refresh_item`) — that fail-soft is correct and stays. The
bug is upstream of it: the product was never requested, so the degrade path is the *only* path a
linked household ever takes.

Consequence for the demo: with two cards both landing at an identical estimated 23% APR,
"highest-APR card" — the engine's target-selection rung — is picking between two indistinguishable
numbers. The one thing a debt-payoff engine most needs to be right about is the one thing it is
guessing.

## The fix

- Add `Products("liabilities")` to the link-token `products` list (`link.py:68`). Weigh
  `optional_products` vs `products`: a hard `products` entry makes Link fail on institutions that do
  not support liabilities, so if that matters for the eventual real-bank rung, request it as
  optional and let `ingest_account_state`'s existing degrade handle absence. For the Sandbox demo,
  either works; document which and why.
- No sync change needed — `ingest_account_state` already reads and stores liabilities when present
  (migration 0014, `plaid_liabilities`); it simply has never had rows to read.
- Re-link / re-sync `hh_demo_plaid` (Sandbox `user_good` returns real liability terms) and confirm
  `apr_source` moves off `ESTIMATED`.

## Acceptance criteria

- [ ] The link token requests `liabilities` (as `products` or `optional_products` — decision noted
      in the ticket/PR).
- [ ] After a re-sync, `hh_demo_plaid`'s `plaid_liabilities` is non-empty and its derived cards
      carry `apr_source = REPORTED` (not `ESTIMATED`) with issuer statement balance and minimum.
- [ ] A household on an institution that does **not** support liabilities still links and syncs
      (the degrade path is preserved) — a test or a documented Sandbox check.

## Notes

- Already recorded as an open follow-up in the [[cfo-ai-link-flow-dogfood]] note ("Add `liabilities`
  to the link products for real card terms") — filed here as a real ticket.
- Complements **`0065`**: this fixes the card's *terms*; `0065` fixes the card's *observed
  behavior*. A linked household needs both before a sweep is honest, but they are independent changes
  and can land in either order.
