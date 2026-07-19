---
id: 0009
title: Payments correctness — the known risks the rail does not yet cover
anchor: ADR-0009
status: accepted
date: 2026-07-18
supersedes:
superseded-by:
---

## [1] Context

The sweep-execution rung is built in shadow: a two-leg saga behind the `TransferProvider` port, an
append-only transfer ledger, a deterministic-key idempotency guard, a signature-verified webhook
boundary, and a reconcile poll backing up the webhooks ([ADR-0006](./0006-the-fbo-commitment-and-the-transfer-webhook-lookup.md),
[ADR-0007](./0007-the-debit-rail-and-the-timing-model.md)). It moves no money — `submit()` is a
no-op — so none of the gaps below has bitten yet.

A review of the built rail against the standard payments-correctness surface — scale, a balanced
ledger, exactly-once, and event/notification correctness — found that the pieces already built are
strong on one axis and largely silent on two others. This ADR records that finding so it is not
rediscovered later, ranks the gaps, and fixes a **posture** for each: accept-and-defer, with the
concrete trigger that promotes it to must-build. It changes no code and no earlier decision.

The organizing insight, which is why this is worth an ADR and not a ticket dump:

> The rail is **concurrency-correct** and **accounting-incomplete**. The hard, well-solved half is
> "two racers, a crash, and a redelivered webhook do not double-move money." The hard, unbuilt half
> is "the books balance against the bank, and a bounced debit has somewhere to go." **Risks R1–R3
> detonate on the first *returned debit*, not at scale** — they are latent in the one-sweep happy
> path already built, not deferred consequences of growth.

### [1.1] What is already correct (the baseline this ADR does not disturb)

Stated so the register below is read as *gaps beside real strengths*, not a verdict on the rung:

- **Exactly-once submit across a crash.** The idempotency key is **deterministic from the slot**
  (`{household}-{decision_date}-{leg}-submit`, derived identically in all three adapters), reused on
  retry. A crash between `provider.submit` and the `submitted` ledger append re-enters with the
  **same** key, and the vendor collapses it — the classic dual-write double-debit window is closed,
  not merely narrowed.
- **Concurrent first-submits.** A `pg_advisory_xact_lock` on the slot serializes two racers that
  would each find the slot empty (a UNIQUE is ruled out by the append-only ledger, KTD-2).
- **Webhook trust and replay.** Signatures verified and timestamps freshness-checked before the
  payload touches the ledger; redeliveries dropped on `(provider, event_id)`.
- **Out-of-order status.** `apply_status` re-reads the provider's *current* truth rather than
  trusting the webhook's payload state, and never re-advances a terminal transfer.
- **Webhook-insufficiency backstop.** A reconcile poll walks non-terminal transfers against the
  provider, catching a doorbell that never rang.

## [2] Decision

Accept the risks below as **known and deferred**, each with an explicit trigger that ends the
deferral. Ranked by depth (R1 deepest). Severity is *impact if hit*; Trigger is *what makes it
must-build*.

### [2.1] R1 — The ledger is a state log, not a double-entry book; the FBO cannot be proven

**Gap.** Each ledger row records one leg's *state transition*. There are no offsetting entries, no
debit=credit invariant, no account whose balance is maintained. So the question that defines a
custody service — "does the cash we believe we hold in the FBO equal the bank's number?" — has no
answer in the schema. `reconcile.py` reconciles *transfer state against the provider's transfer
status*; it does **not** reconcile *FBO cash against the bank statement* (camt.053 / BAI2). Those are
different jobs, and the one that catches "we are short $4,000" is absent. Relatedly, money resting in
the FBO between debit-`settled` and payoff-`settled` lives only implicitly in leg states — there is
no clearing / in-transit account, so a payoff that fails after its debit settled leaves the ledger
saying "payoff: failed" and nowhere saying "customer is owed $X, held in FBO."

**Why hard.** Double-entry plus bank reconciliation is a subsystem (sub-ledger, GL mapping, suspense
and break accounts, a daily statement-ingest job), not a column. It is also the accountant's — and a
regulator's — definition of correct.

**Severity.** Highest. This is the balance guarantee for a custody (ADR-0006) service.
**Trigger.** Before the first live dollar moves, or before any external audit / partner-bank
diligence — whichever comes first. Not deferrable to "at scale."

### [2.2] R2 — The deterministic slot key forbids re-presentment after a return

**Gap.** The same determinism that closes the crash window (R-baseline) makes the slot
`{household}-{date}-{leg}` unique **forever**. NACHA permits re-presenting a debit returned R01/R09
up to twice; each re-presentment is a *new* transfer at the vendor but the *same* logical slot — so
the shared key would dedup the legitimate retry as the original. The model has no "attempt N" axis.
The same boundary blocks two intentional sweeps for one household in one day.

**Why hard.** The fix is a schema change to the idempotency identity (add an attempt/sequence
dimension) that must *not* reopen the double-submit window it currently closes — a careful change,
not a mechanical one.

**Severity.** High. **Trigger.** The first bounced debit — i.e. the moment the debit rail runs live
against anything but a clean sandbox.

### [2.3] R3 — Return codes are captured but not classified into actions

**Gap.** `handle_return` stores `return_code`, and `returned` is a single terminal state. But R01
(NSF — retryable, ≤2×), R10 (unauthorized — never retry; it is a Reg E dispute), and R02/R03
(account closed/invalid — terminal, refund) all collapse to the same "returned." The code→action
mapping — the fork between re-present, dispute, and write-off — does not exist.

**Why hard.** More policy than plumbing: each branch pulls in a downstream flow (re-presentment
counter for R2, dispute/provisional-credit timers for R10, customer refund for R02).

**Severity.** High (a wrong branch is either a compliance miss or a lost dollar).
**Trigger.** Same as R2 — the first return. R2 and R3 are the same event seen from two angles and
should be built together.

### [2.4] R4 — The reconcile poll does not scale

**Gap.** `run_reconcile` is a sequential full-table sweep: every household, every non-terminal
transfer, one vendor API call per ref, with **no per-transfer backoff** (it re-polls a two-hour-old
ACH every cycle), **no sharding**, and **no rate limiter or circuit breaker** around the vendor
calls. Correct at four households; at scale it overruns its window and earns 429s. Separately, the
current state of a transfer is re-derived from the event log (`ORDER BY seq DESC` / `DISTINCT ON`) on
every webhook and every poll — there is no maintained current-state projection. All writes funnel
through one Postgres primary; sharding by household is the natural escape and is undesigned. (Minor,
correctness-safe: `hashtext` is 32-bit, so unrelated slots occasionally share an advisory-lock
integer and needlessly serialize.)

**Why hard.** It is a rewrite of the poll into a due-for-check work queue (watermark + exponential
backoff per transfer), plus provider-level token buckets and a materialized latest-state row.

**Severity.** Medium (degrades throughput and provider standing; does not mis-move money).
**Trigger.** Household count or daily transfer volume crossing the point where a poll cycle no longer
finishes inside its interval — measure it; do not guess.

### [2.5] R5 — Outbound notification correctness is a subsystem that does not exist

**Gap.** The dedup and idempotency guarantees protect the *ledger*; nothing protects the customer's
*inbox*. There is no receipt/notification path, so none of its correctness properties are addressed:
notification idempotency (one "completed" message despite a redelivered webhook), ordering (never
"completed" before "started"), or a sent-notification record. "Keeping notifications right" is
currently only true of the ingress side.

**Severity.** Medium. **Trigger.** The first customer-facing send in the transfer path.

### [2.6] R6 — No velocity, limit, or exposure controls

**Gap.** Nothing caps per-transfer size, per-household daily volume, in-flight exposure, or the total
prefund float ADR-0007 admits. These are the blast-radius bound for when something *is* wrong —
independent of whether any single transfer is individually correct.

**Severity.** Medium-high (a bug without a cap is an unbounded bug). **Trigger.** Before `submit()`
stops being a no-op.

### [2.7] Two smaller ingress hardenings (noted, not ranked)

- Webhook dedup is `(provider, event_id)` **NULLS NOT DISTINCT**; a provider that ever omits
  `event_id` either collapses distinct events or lets replays through. Wants a content-hash fallback.
- `apply_status` enforces monotonicity only at the **terminal** boundary; among non-terminal states
  it does not validate that a transition is forward-legal. A legal-transition check would harden it
  against a buggy status source.

## [3] Consequences

- The ranking is now explicit: **R1 (balanced books), R2 (re-presentment), R3 (return-code actions)
  are gating for the first live dollar**, not for scale. The sweep rung's "live-sandbox hard gate"
  (ADR-0007 [3]) is necessary but **not sufficient** for promotion — it proves the vendors move
  money; it does not prove the books balance or that a return has anywhere to land. This ADR adds
  R1–R3 to what "ready for a real dollar" means.
- R2 and R3 share a trigger (the first return) and should be one work item; R1 is its own subsystem;
  R4/R5/R6 are genuinely deferrable against measured triggers.
- No code changes here. The strengths in [1.1] are recorded precisely so this register is not misread
  as "the rail is broken" — it is concurrency-correct today and must become accounting-correct before
  it is anything more than shadow.
- Follow-up: file the tickets for R1–R3 against the sweep-execution rung; leave R4–R6 as trigger-gated
  entries so they surface when their measurement or milestone arrives.
