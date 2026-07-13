# Architecture

> Evergreen system overview — **edit in place** (unlike append-only ADRs). Headings carry
> `[N]` / `[N.M]` anchors so any part is greppable (`grep -n "\[2\]" docs/architecture.md`)
> and referenceable as `ARCH#2`.

anchor: ARCH

**Version:** v1 (2026-07-12). Supersedes the v0 advice-only design (see git history —
it planned Temporal, an event-driven pipeline, an append-only ledger, and a
provider-abstraction layer for a product that showed the user a number and moved no
money).

**Status:** proposed. Only [`engine/`](../engine) exists. Everything else here is intent,
and should be read as *what we would build and in what order*, not as description.

---

## [1] Overview

The system moves a household's surplus cash onto their most expensive debt, daily,
without asking — and refuses to move anything on days it cannot be sure. See
[`prd.md`](./prd.md).

### [1.1] Principles

1. **The decision is deterministic.** No LLM, no clock, no randomness anywhere near a
   dollar amount. The LLM narrates the decision's reasons; it never produces them.
2. **Refusal is the default.** Every gate fails closed. A day with no sweep is the system
   working.
3. **Inputs are snapshotted, not referenced.** Plaid rewrites history beneath us; a
   decision that cannot be replayed cannot be explained or audited.
4. **Money arrives late and small; it leaves early and large.** Every uncertainty resolves
   against the user's interest.
5. **Build the smallest thing that can be wrong in public.** Shadow mode before sweeps,
   small caps before large ones.

### [1.2] What we are deliberately not building yet

Cut from the v0 design, and why:

| Cut | Why |
| --- | --- |
| **Temporal** | It earns its keep on durable multi-step sagas with compensation — i.e. the ACH payment flow. Adopt it *when money moves* ([5]), not for a daily scheduled job. |
| **Redis** | No cache-shaped access pattern exists. One decision per user per day. |
| **Cloud Storage** | Nothing stores blobs. |
| **`FinancialProvider` abstraction** | MX and Finicity are hypothetical. You cannot design the seam from n=1 — you will guess wrong and reshape it anyway. Call Plaid directly; extract the interface when a second provider is actually underway. |
| **Notification service as a component** | Not a feature in the PRD. A sweep confirmation is an email/push call, not a subsystem. |
| **"Financial ledger"** framing | Accounting-system vocabulary for a system that (today) moves no money. What we need is narrower and stronger: an append-only **decision log** ([3.3]). |

---

## [2] Shape

```
Plaid ──webhook──> ingest ──> sync (cursored pull) ──> normalize ──> Postgres
                                                                        │
                              Cloud Scheduler ──daily──> build Snapshot ┘
                                                              │
                                                          engine.decide()   ← deterministic
                                                              │
                                              ┌───────────────┴───────────────┐
                                          REFUSE                           SWEEP
                                              │                               │
                                       log + explain              payment saga ([5])
                                                                              │
                                                                    log + explain + notify
```

Thinnest stack that ships this: **Next.js** (web) · **FastAPI on Cloud Run** (API +
jobs) · **Postgres/Cloud SQL** (system of record) · **Cloud Scheduler** (the daily run) ·
**Clerk** (auth) · **Secret Manager** · **Sentry**. Add anything else only when a measured
problem demands it.

---

## [3] Components

### [3.1] Ingest & sync

The webhook is a **doorbell, not a delivery**. Plaid webhooks are at-least-once and
unordered, so the handler does exactly two things: persist the raw payload under a natural
dedup key (`item_id` + `webhook_code` + cursor), and enqueue. It never does work inline —
a slow handler causes retries, which cause the duplicates you are trying to avoid.

All state comes from a **cursored pull** (`/transactions/sync`), which makes redelivery a
no-op. A **nightly reconciliation poll** runs for every item regardless of webhooks,
because deliveries *will* be missed and we would rather find out in hours than when
someone's rent bounces.

Every item carries `last_successful_sync_at`. That value becomes `Account.balance_age_days`
in the engine — the freshness gate is the thing standing between a dropped webhook and an
overdraft.

### [3.2] Normalization

The two hard problems, each of which corrupts the forecast *silently* if wrong:

- **Pending → posted reconciliation.** `pending_transaction_id` when the institution
  provides it; otherwise fuzzy-match on account, sign, amount (tolerance asymmetric —
  posted ≥ pending is normal), date window, and normalized merchant. **An ambiguous match
  is not a match**: keep the pending charge alive. Double-counting makes us under-sweep;
  dropping makes us overdraft. The error is one-directional and the design follows it.
- **Internal transfer detection.** Pair-match opposite signs across the user's own
  accounts. Critically, **our own sweeps must reconcile against our own payment records
  before they come back through Plaid** — otherwise they score as discretionary spending,
  the forecast concludes the user is burning cash, and the product slowly strangles itself.

Feeds the **recurring-event engine**, which is where `CashEvent.confidence` comes from.
It needs ~2 observed cycles of an obligation to know anything, hence the engine's 60-day
cold-start gate.

### [3.3] The decision log — append-only, and the reason auditability is real

Each run persists the **entire frozen `Snapshot`**, the `Decision`, and the engine version.

This is the load-bearing design choice in the system, and it buys three things at once:

1. **Explanation that stays true.** "Why did you move $220 that Tuesday" is answerable
   forever, even after Plaid has replaced the underlying transactions.
2. **Audit.** GLBA/UDAAP: show a regulator exactly why this consumer was debited.
3. **Backtest.** Replay any new engine version across every historical snapshot and ask:
   *would this have overdrafted anyone?* That is shadow mode ([6]), and it exists only
   because of this choice.

Store the inputs, not references to the inputs. The difference between an audit trail and
a story.

### [3.4] The engine

[`engine/`](../engine) — built. See [`decision-engine.md`](./decision-engine.md).

Pure function: `Snapshot → Decision`. No I/O.

### [3.5] Explanation

The LLM renders `Decision.reasons` into prose. It receives the reasons, not the raw
financial data, and it is never in the decision path.

**Transaction descriptions are attacker-influenced text** (a merchant controls the string
that appears on a statement). Treat all of it as untrusted: constrain LLM output to a
fixed schema, and never let LLM output touch control flow.

---

## [4] Data model (sketch)

`users` · `items` (a Plaid connection, with `last_successful_sync_at` and connection
state) · `accounts` · `transactions` (with `pending_transaction_id`, a `reconciled_with`
link, and an `internal_transfer_pair` link — **append-only, corrections are new rows**) ·
`recurring_events` · `debts` · `decisions` (the frozen snapshot + output + engine version)
· `payments` (the state machine, [5]) · `policies` (buffer, caps, blackouts).

Every table is scoped by `user_id`, enforced at the repository layer **and** by Postgres
row-level security. An IDOR here exposes someone's complete financial life; one forgotten
`WHERE` clause is not an acceptable single point of failure. Plaid access tokens are
envelope-encrypted with a KMS key, never a plaintext column beside everything else.

---

## [5] Money movement (not yet built)

There is **no universal "pay this card" API.** Card networks are not a repayment rail;
each issuer decides what it accepts. Options, in increasing order of pain: deep-link
handoff → bill-pay partner → FBO/custodial account with a bank partner (heaviest
compliance; avoid as long as possible).

Never call `make_payment()` from a scheduled job against a live balance. The state machine
is the product:

```
proposed → authorized → submitted → pending → settled
                              ↘ returned / failed / cancelled
```

- **Idempotency key** derived from `(user, date, decision_id)`. A retry after a timeout
  must never double-debit — the highest-stakes idempotency in the system, because a
  duplicate sweep *is* an overdraft.
- **Pre-flight re-check immediately before submission.** The decision may be hours old:
  re-run the freshness gates. A decision is a proposal; authorization is a separate,
  fresher act.
- **Returns are normal** (R01 insufficient funds, R02 closed, R03 no account). A payment
  can unwind days after we told the user it happened. The explanation surface and the
  ledger must both be able to say *"this reversed."*
- **This is where Temporal earns its place** — durable execution, retries, compensation.
  Not before.

---

## [6] Build order

1. **Shadow mode.** Ingest → normalize → snapshot → decide → log. **Move nothing.** Compare
   each projected low balance against what actually happened. This produces the one thing
   no competitor in this category had before switching the money on: **a measured tail-risk
   number.** It is also what sets the engine's thresholds, which are currently judgment
   ([`decision-engine.md`](./decision-engine.md) §4.1).
2. **Sweep, small caps, guarantee live.** Overdraft reimbursement from the first dollar.
3. **Raise the ceiling** as calibration proves out — the cap is a function of demonstrated
   forecast calibration, never of a growth target.

---

## [7] Decisions to make before the first migration

Each of these changes the schema, so none can be deferred:

1. **Authz pattern** — repository-layer scoping + Postgres RLS, gated by an IDOR test suite.
2. **Plaid token storage** — KMS envelope encryption; rotation and revocation runbook tied
   to Plaid's item-error webhooks.
3. **GLBA + CCPA posture** — GLBA applies from day one regardless of money movement.
   CCPA/CPRA deletion rights are in direct tension with append-only storage; resolve the
   tension in the schema, not after it.
4. **Plaid production-access review** — a real launch gate that asks for exactly the above.
   Track it as a dependency, not a formality.
5. **APR fallback** — Plaid frequently omits APR. User-entered, estimated-with-lower-
   confidence, or refuse to rank (which is what the engine does today).
