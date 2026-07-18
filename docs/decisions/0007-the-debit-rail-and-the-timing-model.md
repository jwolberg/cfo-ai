---
id: 0007
title: The debit rail is Plaid Transfer, and the timing model is early wait-for-clear
anchor: ADR-0007
status: accepted
date: 2026-07-18
supersedes:
superseded-by:
---

## [1] Context

The sweep-execution rung ([`../plans/2026-07-17-002-feat-sweep-execution-rung-plan.md`](../plans/2026-07-17-002-feat-sweep-execution-rung-plan.md))
built the write half behind a `TransferProvider` port with a **working assumption** of Increase for
the debit leg and Method for the payoff. Two questions the plan deliberately left open — which debit
rail, and *when* to pay the card relative to the debit — are now decided. Neither changes the port,
the ledger, the saga's guarantees, or [ADR-0006](./0006-the-fbo-commitment-and-the-transfer-webhook-lookup.md)'s
custody commitment; both are recorded here because neither is derivable from the code.

## [2] Decision

### [2.1] The debit rail is Plaid Transfer. Increase stays as a swappable backup.

Increase is replaced as the *primary* debit rail by **Plaid Transfer**, for reasons specific to this
codebase, not a general judgment that Plaid Transfer is the better ACH provider:

- **It reuses the Plaid item we already hold.** The transport rung links the bank and stores an
  `access_token`; Plaid Transfer debits off that item directly. Increase is **not** a Plaid processor
  partner, so its adapter needed a Plaid **Auth** → raw account/routing-number handoff. Plaid Transfer
  removes that step.
- **It is multi-rail** (ACH, RTP, FedNow, RfP) and exposes **Plaid Signal**, an ACH-return-risk score —
  the input the timing model below needs to bound prefund risk.
- **It reports a real `settled` status.** Increase has none (settlement is derived from a Transaction
  existing + no return in the window, KTD-3). Plaid Transfer's `settled` is direct, so the debit
  adapter is simpler.

The `TransferProvider` port is why this is an adapter swap, not a rewrite: the saga, ledger,
idempotency guard, webhook trust boundary, and `SWEEP_IN_FLIGHT` feedback are unchanged. The Increase
adapter stays in the tree as a **documented backup** (the `transfers.provider` CHECK admits both), so
the choice remains reversible. One wrinkle Plaid Transfer introduces: it has a genuine two-step
`authorization → transfer` flow, so its `authorize()` **is** a vendor call (returns an authorization
ref carried on `Auth`), unlike Increase/Method where `authorize()` is client-side (KTD-3).

### [2.2] The timing model is early wait-for-clear, with a Signal-scored prefund exception.

Paying a card is a debit (pull from checking) then a payoff (land on the card). **There is no
instant, irrevocable way to pull from a checking account** — ACH debit is the only universal pull, and
it is slow (days) and revocable (up to 60 days for unauthorized). RTP/FedNow are instant and
irrevocable but **push-only**; they cannot silently pull. That constraint is permanent, so per sweep
the choice is forced:

- **Wait-for-clear** — submit the debit, and gate the Method payoff on the debit reaching `settled`.
  Safe (never pays with money not collected), but needs lead time; if the sweep is decided too close
  to the statement due date, the payoff lands late and misses the interest saving the sweep exists for.
- **Prefund** — fire the payoff immediately after the debit submits. Hits the due date, but floats our
  own capital, so a return or fraud is a direct loss.

**Decision: default to wait-for-clear, initiated early.** The engine forecasts, so the debit is
triggered with enough lead time before the due date that ACH settles in time — dissolving the tradeoff
for the common case. Prefund is a **bounded exception** for sweeps decided too late to wait, gated on a
low Plaid Signal return-risk score (float only low-risk debits; wait or refuse otherwise). Both paths
run behind one saga flag, and **both are measured in shadow first**: how often lead time exists vs. a
prefund would be needed, and what the prefund exposure would be. That number decides whether prefund is
worth its fraud/compliance surface before it is ever built live.

"Initiated early" is a *scheduling* property owned by the decision→execution trigger (the engine emits
the sweep with lead time); the saga implements the leg sequencing (payoff gated on debit settlement,
or prefund). The scheduler itself is live-wiring, still ahead.

## [3] Consequences

- The debit adapter is simpler (no Auth-number handoff, a real `settled`), and consolidates on Plaid.
- `Auth` gains an optional `authorization_ref` for the two-step Plaid Transfer authorize; Increase and
  Method ignore it.
- The unavoidable residue is unchanged and named: **custody stands** (ADR-0006 — Method pays from a
  platform account regardless of debit rail or timing), and **some fraction of sweeps will still force
  prefund-or-miss-the-date** — that fraction is exactly what shadow mode measures.
- Nothing turns real money on: `submit()` is still a no-op, and the live-sandbox hard gate is still the
  predecessor to promotion.
