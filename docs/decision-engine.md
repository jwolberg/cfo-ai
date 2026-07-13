# The decision engine

anchor: ENG

Reference implementation of the component [`prd.md`](./prd.md) §2 calls the hard part.
Run: `python3 -m pytest tests/ -q`.

This is not a demo. It moves no money and talks to no bank. It exists to pin down the
logic that decides whether moving money is safe — because that decision, not the
plumbing around it, is what the company lives or dies on.

| File | What it holds |
| --- | --- |
| `engine/models.py` | The value types. Money is `Decimal`, dates are inputs, everything is frozen — and the types **refuse to exist** when the data is incoherent ([3]). |
| `engine/forecast.py` | The conservative projection. One rule: *money arrives late and small; it leaves early and large* ([2.3]). |
| `engine/decide.py` | The refusal gates, the buffer, the reserved minimums, the caps, the target card. Emits `Reason` **codes**, never sentences. |
| `engine/interest.py` | What the debt costs and what a sweep saves — the primary KPI ([`prd.md`](./prd.md) §5.1) and the sentence the user reads, computed in one place ([7]). |
| `engine/outcome.py` | Grades a decision against what actually happened. The calibration asset ([8]). |
| `engine/explain.py` | The only file in the engine that contains copy ([1.1]). |
| `tests/test_decide.py` | The spec. Mostly the ways the real world breaks the happy path. |
| `tests/test_interest.py` | Hand-computable amortization, and the three ways we decline to claim. |
| `tests/test_outcome.py` | The grader. Mostly the ways a metric can quietly lie to you. |
| `tests/test_explain.py` | Every code renders; no refusal reads like an error. |

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
| Discretionary spend | the **p90**, every day — **and this one is measurably too conservative; see [6.5]** |
| Pending debits | already gone |
| Pending credits | not yet money |
| Our own unsettled sweeps | already gone (the bank may not have taken it yet) |

A forecast that is right *on average* overdrafts half its users half the time. The
asymmetry is the entire safety property.

The constraint is the **projected low balance**, not the ending balance. The user only
has to be broke once.

### [2.4] Refusal is the product

`decide()` is mostly a list of reasons to do nothing. Every one is a `ReasonCode`:

| Code | Why we do nothing |
| --- | --- |
| `FUNDING_ACCOUNT_MISSING` | We can't see the account the debit would leave from. |
| `FUNDING_ACCOUNT_NOT_CHECKING` | The debit has to come from checking. |
| `CONNECTION_UNHEALTHY` | Reauth needed. `ITEM_LOGIN_REQUIRED` is a steady state, not an edge case. |
| `BALANCE_STALE` | More than 2 days old. A stale balance is a guess. |
| `INSUFFICIENT_HISTORY` | Under 60 days — you cannot detect a monthly obligation from three weeks of data. |
| `INCOME_TOO_VARIABLE` | Above 0.25 coefficient of variation. **See [5] — this is the one that matters.** |
| `BLACKOUT` | The user paused sweeps for today. |
| `SWEEP_IN_FLIGHT` | ACH is not instant, and stacking is how you overdraft someone with their own money. |
| `NO_DEBT` | The happiest refusal. |
| `APR_UNKNOWN` | Rates unknown *and* more than one card, so we cannot tell which is costing them most. |
| `NO_SURPLUS` | Nothing above the buffer and the reserved minimums. |
| `BELOW_MIN_SWEEP` | What's left isn't worth the ACH risk. |

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

### [6.5] The discretionary-spend model over-reserves — measured, not suspected

*(Anchor appended out of sequence, per the numbering convention: this sits here because it is
the first empirical answer to [6.1] above, not a new gap.)*

`daily_discretionary_high` is a **p90 of DAILY spend**, and `forecast.py` charges it on every
one of the 30 horizon days ([2.3]). Compounding a per-day quantile is not a horizon quantile:
variance grows with **√t**, and this model grows it with **t**.

Measured against three years of synthetic households' own enumerated 30-day windows
(`tests/test_spend_model.py`, `sim/`):

| Household | Engine assumes | Real p99 | Worst 30 days **ever** | Over-reserved vs p99 |
|---|---|---|---|---|
| Typical | $3,101.70 | $2,128.34 | $2,231.26 | $973.36 |
| High-variance | $4,461.00 | $4,058.78 | $4,439.43 | $402.22 |
| Steady | $2,403.30 | $1,785.33 | $1,870.57 | $617.97 |

In every case **the engine reserves more than the household has ever spent in three years.**
The over-reservation is comparable to the entire default $750 buffer, so on many days it is
the whole difference between sweeping and refusing.

Note the shape, because it is [3.1]'s again: it fails **safe**. Nobody is overdrawn, nothing
throws, no alert fires. The product simply refuses more often than it should, quietly,
forever — and the only instrument that would ever reveal it is `false_refusal_cost`, which is
why that metric is a first-class part of the grader rather than a nice-to-have.

**Deliberately not fixed.** The fix loosens the forecast and buys bigger sweeps, which is the
one direction [3] forbids without evidence — and the evidence is precisely what the harness is
being built to produce. Sequence: land the grader and the replay driver, ship the new spend
model with its dial set to reproduce today's refusals, then loosen it only as far as the
*measured* breach rate licenses. Fixing it first would trade a measurable, safe error for an
unmeasured, unsafe one.

Write-up: [`learnings/2026-07-13-the-spend-model-over-reserves.md`](./learnings/2026-07-13-the-spend-model-over-reserves.md).

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

## [7] What the sweep saved

`engine/interest.py` computes the number in [`prd.md`](./prd.md) §1 — *"that's $31 of
interest you won't pay"* — and the number in §5.1 that the company is graded on. They are the
same number, so exactly one place computes it.

**The counterfactual is the household's own payment trajectory, not the card minimum.** §5.1
says so explicitly, and the distinction is load-bearing: users of this product already pay
*more* than the minimum — that is *why* they have idle cash — so measuring against the
minimum would credit our sweep with interest they were never going to pay anyway. That
inflates the one metric the company reports, which is the exact KPI failure §5.1 was written
to ban. So `Debt.observed_monthly_payment` is the baseline, `minimum_payment` is not an input
to the interest math at all, and a test asserts that changing it cannot move the claim by a
cent.

Interest accrues **daily** (average-daily-balance, no intra-cycle compounding) because the
product sweeps daily — a monthly amortization would value a sweep on day 2 and one on day 29
identically, and be wrong in the direction of over-claiming.

### [7.1] Three ways the engine declines to say what it saved

The claim is a `Reason` like any other, so its **absence** is the mechanism — there is no code
path that can render an invented number.

| Condition | Why we say nothing |
| --- | --- |
| APR unknown | Plaid does not report it for many issuers ([6.3]). A figure that looks computed but was invented is worse than no figure. |
| No observed payment history | We will **not** fall back to the minimum — that is the flattering assumption above. Costs nothing in practice: `INSUFFICIENT_HISTORY` already blocks sweeps under 60 days, so by the time we may move money we have seen two payment cycles. |
| Their payments don't cover their interest | There is no payoff, so there is no interest total. The model *raises*; the decision path catches it and claims nothing. The sweep is still safe and still happens. |

### [7.2] The claim runs to payoff, and it is bigger than you expect

A $300 sweep against a $9,000 card at 23.99%, for a household paying $400/month, avoids
**$236.94**. That is arithmetically right — the card takes ~31 months to clear, so $300 of
principal removed today escapes ~2.6 years of compounding at 24%.

It is also **7.6× the illustrative figure in [`prd.md`](./prd.md) §1** ("$220 → $31"). One of
the two is wrong, and it is worth settling before this number is shown to a customer: a
to-payoff claim is honest but leans on the household maintaining its payments for years, and a
large number invites exactly the disbelief this product cannot afford. A bounded-horizon claim
("over the next 12 months") would under-claim and be checkable against a real statement.

Per-sweep claims **do** compose: each day's figure is marginal against that day's actual
balance and assumes no further sweeps, so the daily claims telescope to the true total against
never-sweeping. They are not double-counted.

## [8] Grading the decision

`decide()` emits a prediction. `engine/outcome.py` settles the bet — and until it existed,
nothing in this repo ever did. The engine's central claim, *"this $220 is not needed"*, had
never once been checked against the future that followed it.

[`strategy.md`](./strategy.md) §3: the only asset that compounds is **calibration** — "the
empirical distribution of our own errors." This is that distribution's data structure, and it
is what makes every future change to the forecast **defensible rather than merely plausible**.

### [8.1] The signature is the safety property

`grade()` does not take "the realized balances". It takes the household's own daily cash
movement — **excluding anything we did** — and applies the decision's sweep **itself**.

A replay that grades each decision against the household's *untouched* history never compounds
the effect of its own sweeps, and systematically **understates** breach risk: it reports the
tail we would have had if we had never acted. That is the difference between a shadow-mode
report and a shadow-mode lie, and it is not the sort of thing to leave to a caller's diligence.
The caller cannot forget to apply the sweep, because the caller is not the one who applies it.

Our money is assumed to leave on the **day we decided**, not when ACH would plausibly have
posted it — the assumption most likely to *find* a breach rather than excuse one. A grader that
flatters itself is worse than no grader.

### [8.2] What it measures, and why each one is a different fact

| Field | Why it exists |
| --- | --- |
| `projection_error` | `realized_low_unswept − projected_low`, **signed**. The calibration asset. Measured against the household's *unswept* path: `projected_low_balance` projects their pre-sweep trajectory, so grading it against the post-sweep low would fold the size of our own sweep into the "error" — making the engine look wildly optimistic on exactly the days it swept hardest and was right. |
| `sweep_caused_overdraft` | [`prd.md`](./prd.md) §5.2's guardrail — the hard gate that outranks the KPI. Distinct from `overdrafted`: a household that goes below zero **on its own** is not our doing, and folding those in would drown the metric in lives we never touched. True only when `realized_low < 0 ≤ realized_low_unswept`. |
| `false_refusal_cost` | §5.3's "cost of conservatism" — but **ours**, not the user's. Computed as `would_sweep(realized_low) − swept`: what `decide()` would have moved with a perfect forecast, obeying every cap the user set. If they capped us at $300 and we moved $300, we were not being conservative, we were being *obedient*, and this is zero however much idle cash hindsight reveals. |
| `interest_claimed` | What we told them it bought. Whether it was *realized* depends on their payments over years and belongs to the KPI layer, not a 30-day grade — see [7.2]. |

`engine/decide.py` exports `untouchable()`, `apply_caps()` and `would_sweep()` so the grader
applies the **identical** money rules the engine shipped. Two copies of that arithmetic would
drift, and the day they drifted the calibration numbers would start describing an engine that
never existed.

Uncapped, the metrics satisfy an exact identity: **`false_refusal_cost == projection_error`**.
The forecast error, priced in dollars. It is asserted as a test — if they ever drift apart under
an uncapped policy, one of them has a bug.

### [8.3] The per-sweep cap currently **masks** the spend-model bug

Under the default **$300** cap, [6.5]'s over-reservation costs the user *nothing measurable*.
The cap binds long before the forecast error does, `false_refusal_cost` reads **$0.00**, and a
shadow-mode report run at production caps would pronounce the engine healthy.

The bug is **latent, not absent**. Lift the cap on the same household, the same day, the same
realized future, and it prices out at **$867.13** immediately.

Which means it starts costing real money precisely when the ceiling is raised — and
[`prd.md`](./prd.md) §8's *"Then: raise the ceiling as calibration proves out"* is exactly the
plan to raise it. **The moment the company begins trusting its calibration is the moment this
bug begins to bite.**

So the replay driver — the last unbuilt piece of the shadow-mode harness — **must sweep the cap
across a range.** Run at production caps alone, it will measure a forecast error of zero and
issue a false clean bill of health. There is a test that says so.
