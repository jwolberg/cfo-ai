---
id: "0066"
title: The Plaid Link token requests only `transactions` — so every linked card's APR, statement and minimum are estimated, not reported
type: fix
status: in-progress
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

- [x] The link token requests `liabilities` (as `optional_products`, not a hard `products` entry —
      see Decision below). Pinned by `tests/test_link_loop.py::test_link_token_requests_liabilities_as_an_optional_product`.
- [ ] After a re-sync, `hh_demo_plaid`'s `plaid_liabilities` is non-empty and its derived cards
      carry `apr_source = REPORTED` (not `ESTIMATED`) with issuer statement balance and minimum.
      **Operational step — pending a re-link/re-sync of `hh_demo_plaid` against Plaid Sandbox** (the
      existing Item was linked *before* this change and never consented `liabilities`; re-linking is
      required for the issuer to return terms). See Notes.
- [x] A household on an institution that does **not** support liabilities still links and syncs
      (the degrade path is preserved). Covered by
      `tests/test_plaid_accounts.py::test_liabilities_absent_degrades_to_balances_only` and
      `tests/test_sync.py` (`liabilities absent → balances-only, not a failure`); `optional_products`
      guarantees Link itself does not fail on a non-supporting institution.

## Decision

Requested as `optional_products=[liabilities]`, not `products`. A hard `products` entry makes Link
**fail** on any institution that does not support liabilities; optional lets a supporting institution
consent it while a non-supporting one degrades cleanly (AC3), which is the fail-soft
`ingest_account_state` has always relied on. For the Sandbox demo either would work; optional is the
shape the eventual real-bank rung needs, so it is chosen now.

## Notes

- Already recorded as an open follow-up in the [[cfo-ai-link-flow-dogfood]] note ("Add `liabilities`
  to the link products for real card terms") — filed here as a real ticket.
- Complements **`0065`**: this fixes the card's *terms*; `0065` fixes the card's *observed
  behavior*. A linked household needs both before a sweep is honest, but they are independent changes
  and can land in either order.
- **AC2 is the live half and requires an operator step** (2026-08-15). The code change only affects
  *new* link tokens; `hh_demo_plaid`'s Item already exists and was linked without `liabilities`, so
  its terms stay `ESTIMATED` until it is re-linked (a fresh `/plaid/link/token` → Link → `/exchange`)
  or the Item re-consented, then re-synced. `apr_source` moving off `ESTIMATED` is verified there,
  against Neon — not something the test suite can prove. Sandbox `user_good` returns real liability
  terms, so the re-link is the whole check.
