# The decision engine

anchor: ENG

Reference implementation of the component [`prd.md`](./prd.md) §2 calls the hard part.
`engine/` + `tests/test_decide.py`. Run: `python3 -m pytest tests/ -q`.

This is not a demo. It moves no money and talks to no bank. It exists to pin down the
logic that decides whether moving money is safe — because that decision, not the
plumbing around it, is what the company lives or dies on.

## [1] The shape of it

```
Snapshot ──> forecast.conservative_low_balance ──> decide ──> Decision ──> explain
(frozen         (worst plausible trajectory)      (gates,     (sweep|refuse,   (prose)
 inputs)                                           caps,       + Reason codes)
                                                   target)
```

Deterministic end to end. No LLM, no clock, no network, no randomness. The same
`Snapshot` yields the same `Decision` forever.

### [1.1] Reasons are codes, not sentences

`decide()` emits `Reason(code, params)` — never English. Prose lives in one place,
`engine/explain.py`, and is the only file in the engine that contains copy.

This is not tidiness. Four things fall out of it:

- **Copy edits cannot break a financial calculation.** Changing "paused" to "snoozed" is
  a change to the renderer; the test suite asserts on `ReasonCode.BLACKOUT` and doesn't
  care.
- **The audit log stays stable.** A decision persisted in 2026 still means the same thing
  in 2029 after three rounds of UI copy. The code is the record; the sentence is a view.
- **The LLM narrates *from* the codes**, rather than passing a finished string through.
  Handed a code and its parameters it can write for this particular household; handed a
  sentence, all it can do is paraphrase — and any paraphrase of a financial claim is a
  chance to change its meaning.
- **Translation is possible at all**, instead of being a rewrite of `decide()`.

A code with no copy fails in CI (`test_every_reason_code_has_copy`), not in front of a
customer whose money has just moved. And a refusal must never read like an error — the
user is not being denied something, they are being told their money is staying put and
why. That's asserted too.

## [2] The four rules that matter

### [2.1] The engine never reads a clock

`today` is a field on the input, not a call to `date.today()`. Everything else follows
from this: the sweep is replayable, backtestable, and explainable *after the fact* —
which matters enormously because Plaid mutates history underneath you. Pending
transactions are replaced with different IDs; banks reverse and restate postings. If
the engine read a clock or re-fetched, "why did it say $220 that day" would become
permanently unanswerable, and the audit trail the whole trust story rests on would be
fiction.

The `Snapshot` is captured with each decision. That is the audit record.

### [2.2] Only the funding account protects the user

An ACH debit leaves **one** account. A household with $100 in checking and $5,000 in
savings has $5,100 of money and **$100 of protection** — summing them and testing the
total against the buffer authorises a sweep that overdraws checking while the savings
sits untouched. Savings is real money, but it is not *there*, and moving it is a second
ACH with its own delay and its own failure modes.

So `conservative_low_balance` projects the funding account alone. Events and pending
transactions are attributed to accounts and filtered: income paid into savings does not
fund a checking-account debit. The refusal gates apply to the funding account too — a
stale *savings* balance cannot overdraw checking, so refusing on it would be superstition
rather than safety.

The idle savings is not ignored, though. When there is meaningful cash sitting outside
the funding account, the engine says so ([4]) — it just won't act on it.

### [2.3] Money arrives late and small; it leaves early and large

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

### [2.4] Refusal is the product

`decide()` is mostly a list of reasons to do nothing:

- an account needs reauth (`ITEM_LOGIN_REQUIRED` is a steady state, not an edge case)
- the balance is more than 2 days stale
- fewer than 60 days of history (cold start — you cannot detect a monthly obligation
  from three weeks of data)
- **income variation above 0.25** — see [5]
- the user's blackout window
- a previous sweep still unsettled (ACH is not instant; stacking is how you overdraft
  someone with their own money)
- no surplus above buffer + reserved minimums
- APRs unknown *and* more than one card, so we cannot tell which one is costing them
  most

Days with no sweep are the feature working.

## [3] Bad data is rejected, never smoothed over

The types refuse to exist when the data is incoherent, because every one of these
silently produces a *wrong dollar amount* rather than an error:

- **`money()` rejects floats.** `Decimal(2.675)` is not 2.675 — it is the binary
  approximation, and it quantizes to 2.67. A silently wrong cent is precisely what this
  type discipline exists to prevent.
- **`CashEvent` rejects mis-signed bounds.** An outflow of `-1800` with `amount_high`
  entered as `+2200` ("could be as much as $2,200") would resolve to `-1800` as the worst
  case, hiding $400 of exposure. Upstream is a not-yet-written recurring-event detector;
  this is the boundary that stops its sign bug from becoming someone's overdraft. It also
  rejects negative jitter, which would invert the whole late-income/early-obligation rule.
- **`Debt` rejects a negative `minimum_payment`** — plausible from a misread signed
  Liabilities field. It would shrink the reserve and hand the user a *larger* sweep.
  Clamping it to zero would stop the overdraft but silently invent a $0 minimum, which is
  the engine guessing at money. It refuses instead, and the sync layer deals with it. It
  also rejects an APR outside 0–200%, which catches the units bug (`24.99` for 24.99%).

The rule: **an upstream data bug must never buy a bigger sweep.**

### [3.1] The minimum payment is reserved once, not twice

`Debt` is the **single authoritative source** for a card's minimum payment, and
`decide()` reserves it out of available cash.

The recurring-event detector will also happily identify that minimum as a monthly
obligation — it looks exactly like one — and emit it as a `CashEvent`. Left
undistinguished, the same payment is subtracted twice: once by the forecast, once by the
reserve. So `CashEvent.kind` carries `DEBT_MINIMUM`, and the forecast skips those events.

Note the shape of this bug, because it is the kind that survives: the direction is
**safe**. Double-counting makes us *under*-sweep — nobody is overdrawn, nothing throws,
no alert fires. The product would simply have refused more often than it should, quietly,
forever, and the metric that revealed it would be a slightly disappointing revenue curve
two years later.

## [4] Idle cash outside the funding account

We won't sweep it. But staying silent while someone holds $20,000 in a savings account
earning nothing and pays 24% on a card is its own kind of failure — so the engine names
it in the decision's reasons.

Note what it does *not* say. It does not suggest investing the money: this user is
carrying revolving debt at 20–30% APR, and paying that down is a guaranteed, tax-free,
risk-free return that no ordinary investment beats. Advising an indebted household into
the market would be a straightforwardly bad trade and a regulatory problem. What it says
is narrower and true: **the buffer has to exist, but it does not have to earn nothing.**

## [5] The refusal that matters most

**High income variance disqualifies the user.**

The behavioral literature (Telyukova 2013; Fulford 2015; Druedahl & Jørgensen 2018) is
clear that much co-holding of card debt and idle cash is *rational precaution* against
volatile income and the risk of a credit line being cut without warning. For a
household with genuinely lumpy income, the buffer is not inefficiency — it is the
correct hedge, and sweeping it is **worse advice than doing nothing**.

So the engine refuses to serve users it cannot forecast, at the direct cost of
addressable market. This is the single most important line in the file, and it is the
one a growth target will eventually come for.

## [6] Known gaps — deliberately not built

### [6.1] Calibration is asserted, not measured

`INCOME_CONFIDENCE_FLOOR = 0.80`, `MAX_INCOME_VARIATION = 0.25`, `MAX_BALANCE_AGE_DAYS = 2`
are judgment, not evidence. The only honest way to set them is shadow mode: run the
engine against real households, move nothing, and measure how often the realized low
balance fell below the projection. Then set the thresholds from the observed tail.

That backtest is the actual first milestone of the company ([`prd.md`](./prd.md) §8),
and the resulting error distribution is the asset that compounds
([`strategy.md`](./strategy.md) §3).

### [6.2] The upstream problems this file assumes away

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

### [6.3] APR is frequently missing

Plaid's Liabilities product does not return APR for many issuers. The engine handles it
by refusing to rank rather than guessing — a wrong target card looks exactly like
working while quietly destroying the entire value proposition. A real product needs a
user-entered fallback.

### [6.4] Money movement

There is no universal "pay this card" API. Card networks are not a repayment rail; each
issuer controls acceptance. Everything downstream of `Decision` — authorization,
idempotency, the ACH state machine, returns, NSF, reconciliation — is out of scope here
and is the other half of the engineering problem.
