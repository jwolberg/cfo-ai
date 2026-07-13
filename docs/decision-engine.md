# The decision engine

anchor: ENG

Reference implementation of the component [`prd.md`](./prd.md) §2 calls the hard part.
`engine/` + `tests/test_decide.py`. Run: `python3 -m pytest tests/ -q`.

This is not a demo. It moves no money and talks to no bank. It exists to pin down the
logic that decides whether moving money is safe — because that decision, not the
plumbing around it, is what the company lives or dies on.

## [1] The shape of it

```
Snapshot ──> forecast.conservative_low_balance ──> decide ──> Decision
(frozen         (worst plausible trajectory)      (gates,     (sweep | refuse,
 inputs)                                           caps,       + reasons)
                                                   target)
```

Deterministic end to end. No LLM, no clock, no network, no randomness. The same
`Snapshot` yields the same `Decision` forever.

## [2] The three rules that matter

### [2.1] The engine never reads a clock

`today` is a field on the input, not a call to `date.today()`. Everything else follows
from this: the sweep is replayable, backtestable, and explainable *after the fact* —
which matters enormously because Plaid mutates history underneath you. Pending
transactions are replaced with different IDs; banks reverse and restate postings. If
the engine read a clock or re-fetched, "why did it say $220 that day" would become
permanently unanswerable, and the audit trail the whole trust story rests on would be
fiction.

The `Snapshot` is captured with each decision. That is the audit record.

### [2.2] Money arrives late and small; it leaves early and large

Every uncertain quantity resolves toward the end of its range that hurts the user:

| Quantity | Assumed |
|---|---|
| Income timing | expected date **+** jitter (late) |
| Income amount | the **low** end |
| Income we're <80% sure of | **not money at all** — excluded |
| Obligation timing | expected date **−** jitter (early) |
| Obligation amount | the **high** end |
| Obligations we're unsure of | still counted — an uncertain bill is still a bill |
| Discretionary spend | the **p90**, every day |
| Pending debits | already gone |
| Pending credits | not yet money |
| Our own unsettled sweeps | already gone (the bank may not have taken it yet) |

A forecast that is right *on average* overdrafts half its users half the time. The
asymmetry is the entire safety property.

The constraint is the **projected low balance**, not the ending balance. The user only
has to be broke once.

### [2.3] Refusal is the product

`decide()` is mostly a list of reasons to do nothing:

- an account needs reauth (`ITEM_LOGIN_REQUIRED` is a steady state, not an edge case)
- the balance is more than 2 days stale
- fewer than 60 days of history (cold start — you cannot detect a monthly obligation
  from three weeks of data)
- **income variation above 0.25** — see [3]
- the user's blackout window
- a previous sweep still unsettled (ACH is not instant; stacking is how you overdraft
  someone with their own money)
- no surplus above buffer + reserved minimums
- APRs unknown *and* more than one card, so we cannot tell which one is costing them
  most

Days with no sweep are the feature working.

## [3] The refusal that matters most

**High income variance disqualifies the user.**

The behavioral literature (Telyukova 2013; Fulford 2015; Druedahl & Jørgensen 2018) is
clear that much co-holding of card debt and idle cash is *rational precaution* against
volatile income and the risk of a credit line being cut without warning. For a
household with genuinely lumpy income, the buffer is not inefficiency — it is the
correct hedge, and sweeping it is **worse advice than doing nothing**.

So the engine refuses to serve users it cannot forecast, at the direct cost of
addressable market. This is the single most important line in the file, and it is the
one a growth target will eventually come for.

## [4] Known gaps — deliberately not built

### [4.1] Calibration is asserted, not measured

`INCOME_CONFIDENCE_FLOOR = 0.80`, `MAX_INCOME_VARIATION = 0.25`, `MAX_BALANCE_AGE_DAYS = 2`
are judgment, not evidence. The only honest way to set them is shadow mode: run the
engine against real households, move nothing, and measure how often the realized low
balance fell below the projection. Then set the thresholds from the observed tail.

That backtest is the actual first milestone of the company ([`prd.md`](./prd.md) §8),
and the resulting error distribution is the asset that compounds
([`strategy.md`](./strategy.md) §3).

### [4.2] The upstream problems this file assumes away

`Snapshot` arrives clean. It will not. Each of these is a real subsystem, and each is a
single word in the dataclass:

- **pending → posted reconciliation** — not reliably 1:1; splits, reorderings, unlinked
  pairs. Get it wrong and a transaction is silently double-counted or dropped.
- **internal transfer detection** — checking → savings misread as income or spending
  corrupts the forecast in a way that looks like nothing is wrong.
- **recurring event detection** — where `CashEvent.confidence` actually comes from.
- **webhook idempotency** — Plaid delivery is at-least-once and unordered.
- **the data freshness gate** — `balance_age_days` implies a sync pipeline that knows
  when it last succeeded, and a reconciliation poll behind the webhooks.

### [4.3] APR is frequently missing

Plaid's Liabilities product does not return APR for many issuers. The engine handles it
by refusing to rank rather than guessing — a wrong target card looks exactly like
working while quietly destroying the entire value proposition. A real product needs a
user-entered fallback.

### [4.4] Money movement

There is no universal "pay this card" API. Card networks are not a repayment rail; each
issuer controls acceptance. Everything downstream of `Decision` — authorization,
idempotency, the ACH state machine, returns, NSF, reconciliation — is out of scope here
and is the other half of the engineering problem.
