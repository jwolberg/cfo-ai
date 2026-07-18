---
id: 0006
title: The FBO custody commitment, and the transfer-webhook tenancy exception
anchor: ADR-0006
status: accepted
date: 2026-07-17
supersedes:
superseded-by:
---

## [1] Context

The sweep-execution rung ([`../plans/2026-07-17-002-feat-sweep-execution-rung-plan.md`](../plans/2026-07-17-002-feat-sweep-execution-rung-plan.md))
is the write half of the product: it debits a customer's checking and pays down their credit card.
Two decisions this rung makes need recording where a future reader will find them, because both leave
the shape of the system and neither is derivable from the code.

**The forcing facts.** Paying a card is two legs — a debit (ACH pull from checking) and a payoff
(landing money on the issuer's card). The verified 2026 payoff channel is a biller-payoff API
(Method), whose payment *source* must be a platform account we hold; an end-user's checking cannot be
a source. So the user's money necessarily transits an account we control. Separately, both providers
report settlement and returns by **webhook**, which names a provider transfer id, not a `household_id`
— and `transfers` is FORCE'd (ADR-0004), so an unscoped read returns nothing.

## [2] Decision

### [2.1] The rung commits to an FBO/custodial posture.

Because the money transits an account we hold, this rung takes on the FBO posture the core docs said
to *"avoid as long as possible"* ([`../architecture.md`](../architecture.md) [5], `../prd.md` [6]),
**knowingly**. That turns on Reg E unauthorized-transaction liability, GLBA safeguards, reconciliation
between the FBO balance and the ledger, and money-transmitter obligations where holding user funds
triggers them — **from the first live transfer**. None of it blocks the *shadow* build (no funds
transit in shadow), but all of it is a predecessor to turning `submit()` on. This presumes a direct /
owned-rail model; `../prd.md` [7.1] warns that an embedded partner may own the rail and the custody,
which would delete this investment — so distribution should settle before real money is spent here.

### [2.2] One transfer-webhook tenancy exception, mirroring ADR-0005.

The saga faces exactly the problem [ADR-0005](./0005-plaid-webhook-tenancy-exceptions.md) solved for
the Plaid doorbell, so it reuses that shape — and, per ADR-0005 [3]'s rule that *any* new unscoped
table or definer function is a new ADR rather than a precedent, records it here:

1. **`transfer_webhooks`, unscoped.** The raw provider-webhook store sits outside `HOUSEHOLD_SCOPED`
   (a webhook names a provider ref, not a household; a scoped INSERT would fail closed). Deduped on
   `(provider, event_id)` **NULLS NOT DISTINCT**, `SELECT/INSERT/DELETE` to `cfo_app`, retention
   purges old rows. No RLS.

2. **`transfer_household_for_provider_ref(text, text) RETURNS text`, SECURITY DEFINER.** The one
   sanctioned read of FORCE'd `transfers` before a scope is set: it maps `(provider, transfer id) →
   household id`, runs as its owner over that single narrow surface, returns a scalar household id and
   nothing else, and has `EXECUTE` granted to `cfo_app`. **Never** `BYPASSRLS` on the app role, which
   would unscope every query it makes — `assert_rls_binds()` refuses that role outright.

These are the **only** exceptions this rung adds. Any further unscoped table or definer function is,
again, a new ADR.

## [3] Consequences

- The saga can resolve a household from a provider webhook without weakening tenancy anywhere else.
- The FBO commitment is now explicit and greppable, with its compliance obligations named as
  predecessors to live money movement — not discovered after the first debit.
- The idempotency guard that protects a debit keys on `(household_id, decision_date)` and is enforced
  by an advisory slot lock, not a UNIQUE (the ledger is append-only); see the plan's KTD-2.
- `../architecture.md` [5] is updated to describe the scoped two-leg rail and the corrected key; this
  ADR is the durable record of *why*.
