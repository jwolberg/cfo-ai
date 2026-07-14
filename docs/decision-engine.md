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
| `backend/replay.py` | The grader's **caller**. Walks a household day by day, decides, and grades every day it can honestly grade ([8]). |
| `backend/calibrate.py` | Grades a **population**, at every setting of the spend dial, and licenses at most one ([6.5]). |
| `tests/test_decide.py` | The spec. Mostly the ways the real world breaks the happy path. |
| `tests/test_interest.py` | Hand-computable amortization, and the three ways we decline to claim. |
| `tests/test_outcome.py` | The grader. Mostly the ways a metric can quietly lie to you. |
| `tests/test_explain.py` | Every code renders; no refusal reads like an error. |
| `tests/test_calibrate.py` | The licensing rule. Mostly the ways a safety bar can license a regression. |

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
| Discretionary spend | the **p90**, every day — **measurably too conservative, and the fix was measured and refused; see [6.5], [6.6]** |
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
| `CADENCE_HOLD` | We paid the card recently. Every sweep is a draw on the tail; the number of draws is itself a risk control. **See [9].** |
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

The rule: **an upstream data change must never buy a bigger sweep.**

> This rule used to say *bug*, and the word was too narrow. Connecting a real credit card is
> **not a bug** — it is the system working correctly, ingesting *better* data. But
> `daily_discretionary_high` is a p90 of **checking** spend, so moving a household's spending
> onto a card collapses that series toward zero, the forecast stops reserving for spend, and the
> engine finds "surplus" that is really just an obligation that has not arrived yet. Better data,
> bigger sweep, same overdraft.
>
> The reason a data *bug* may not enlarge a sweep is that a sweep must never grow for a reason
> unrelated to the household genuinely having more spare cash. A channel shift satisfies that
> description exactly, so the rule now covers it by name. The portfolio reserve
> (`obligation_in_horizon`) is what makes the card case safe: what moves onto the card is
> reserved as an obligation, not silently released as surplus.

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

### [6.1] The thresholds are asserted. The forecast is now measured.

**This gap has split in two, and only half of it is still open.**

`INCOME_CONFIDENCE_FLOOR = 0.80`, `MAX_INCOME_VARIATION = 0.25`, `MAX_BALANCE_AGE_DAYS = 2`
are **still** judgment, not evidence. Nothing here has measured them, and the only honest way
to set them remains shadow mode against **real** households.

What has changed is that the harness those thresholds would be set *from* now exists and has
run. `engine/outcome.py` has a caller ([8]), and `backend/calibrate.py` grades a population
rather than an anecdote. Against **60 synthetic households × 90 days = 4,320 graded days**:

| | |
|---|---|
| Breach rate — days the realized low came in **below** our projection (we were optimistic) | **2.3%** |
| Sweep-caused overdrafts ([`prd.md`](./prd.md) §5.2's guardrail) | **0**, in 590 sweeps |
| False-refusal cost — our conservatism, deferrals excluded ([9.2]) | ~**$544K** |

Read that table with its own caveat attached: it is **one simulator, three spend shapes, twenty
seeds**. It is a demo population, not a prior. It cannot set `MAX_INCOME_VARIATION`, because
`sim/` generates income from the same assumptions the engine forecasts with — a backtest of a
model against its own generator measures the code, not the world.

What it *can* do is catch the engine being wrong in ways a single household hides, and it
immediately did ([8.4]).

That backtest against real households is still the first milestone of the company
([`prd.md`](./prd.md) §8), and the error distribution is still the asset that compounds
([`strategy.md`](./strategy.md) §3). What is no longer true is that the engine has never been
told whether it was right.

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

**Still not fixed — and now for a much better reason than "we haven't measured it yet."**

The sequence this section prescribed has run to completion: the grader landed, the replay driver
landed, the new spend model shipped behind a dial set to reproduce today's refusals, and the
breach rate was measured across a population. **The measurement refused the fix.**

Write-up: [`learnings/2026-07-13-the-spend-model-over-reserves.md`](./learnings/2026-07-13-the-spend-model-over-reserves.md)
(the bug) and
[`learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`](./learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md)
(why the fix does not work).

### [6.6] The fix was measured, and it is not safe to ship

The proposed replacement was non-parametric and, on paper, unimprovable: reserve against the
household's **own** enumerated worst 30-day window (`spend_30d_high`), rather than against
`30 × p90_daily`. No distributional assumption, their real skew, their real autocorrelation.

Measured across the same 4,320 graded days, against today's **2.3%** breach rate:

| Dial (`SPEND_QUANTILE`) | Breach rate | Sweep-caused overdrafts |
|---|---|---|
| **today** — `30 × p90_daily` | **2.3%** | 0 |
| `q = 1.0` — their worst month **ever** | **19.8%** | 1 |
| `q = 0.90` | 24.4% | 1 |

Reserving against the worst 30-day stretch a household has *ever actually had* breaches nearly
**nine times as often** as the model this section calls an over-reserver. Nothing at any setting
is licensed. `SPEND_QUANTILE` ships as `None`, `forecast.py` falls back to
`daily_discretionary_high`, and a test fails if anyone moves the dial without a measurement.

**The model is not wrong. It is starved.** It reads that worst-ever window off the 60–150 days
of history the engine actually has — which is **2–5 *independent* months**, because overlapping
windows flatter the sample count without adding information. *The worst of 3 months is a badly
biased estimate of the worst of 36.* Against three years of ground truth, it estimates the true
worst month at:

| Household | Its true worst 30 days (3y) | What the model estimates |
|---|---|---|
| Typical | $2,059 | $1,580 — **77%** |
| **High-variance** | $4,383 | $2,548 — **58%** |
| Steady | $1,742 | $1,503 — **86%** |

**The bias is worst exactly where it is most dangerous.** A fat tail means the bad month is
*rare*, which means a short history almost never contains one — so the household most likely to
blow up gets a reserve covering **58%** of its true worst month. The error is anti-correlated
with safety.

This is [5]'s trap wearing different clothes. There, σ flatters the skewed household; here, a
short window does. Both instruments are least trustworthy on the household that needs them most.

Given three years of history instead of sixty days, the same model's breach rate falls from
**19.3% to 3.7%**, and on the fat-tailed household to **zero**. The idea was right. The data is
not there.

So the honest options are now: **gate the empirical model on history length** rather than on a
quantile (it beats the incumbent for households with years of data and is dangerous for the ones
we just onboarded — that is a second model with an eligibility rule, not a dial); scale the
estimate to cover the tail it cannot see (but the correction needed is *shape-dependent*, 58% vs
86%, so a single global scalar reintroduces exactly the flattering-the-skewed-household problem
this model was chosen to avoid); or leave it. The incumbent is expensive, but it is expensive in
the **safe** direction, and it is the only model here measured to overdraft nobody.

What is no longer available is shipping the swap because the reasoning is elegant. It was.

### [6.7] A safety bar that only measures safety will license a regression

`calibrate.py:licensed()` originally asked two questions: does this setting overdraft anyone, and
is its breach rate no worse than today's on every shape.

**Both get easier to pass the more you reserve.** They are monotone in the size of the reserve,
so a dial swept far enough in the *tightening* direction eventually satisfies both — and gets
pronounced "licensed" for the sole achievement of being more conservative than the model it
replaces.

`q = 3.0` does exactly this. It clears both bars, and it costs **$1.62M** in false-refusal cost
against today's **$544K** — and the report would have announced that $1.08M regression as a
buy-back of `$-1,078,015.78`.

There is now a third condition: **it has to actually buy something back.** A setting that
reserves more than today is a tightening wearing the name of a loosening, whatever its breach
rate. The generalization is worth keeping: *a metric that only measures the thing you are afraid
of will happily recommend doing nothing at all.*

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

Interest accrues **daily** (average-daily-balance, no intra-cycle compounding) because that is
how the *card* works — a monthly amortization would value a sweep on day 2 and one on day 29
identically, and be wrong in the direction of over-claiming. This is a fact about the issuer, not
about our cadence, and it stays true whatever [9] sets the spacing to. (It used to be justified
here as "because the product sweeps daily," which was circular: the cadence was never chosen.)

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

`outcome.py` then existed for a day and a half and **nothing called it**. `backend/replay.py` is
the caller, and `backend/calibrate.py` runs it across a population. The engine now has numbers
about itself ([6.1]), and the first thing they did was refuse a change everyone expected to ship
([6.6]).

[`strategy.md`](./strategy.md) §3: the only asset that compounds is **calibration** — "the
empirical distribution of our own errors." This is that distribution's data structure, and it
is what makes every future change to the forecast **defensible rather than merely plausible**.
It has now done that job once, in the only way that counts: by saying **no**.

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

### [8.3] The per-sweep cap can **mask** the spend-model bug — and the harness still doesn't sweep it

When this was written the default per-sweep cap was **$300**, and at that ceiling [6.5]'s
over-reservation cost the user *nothing measurable*: the cap bound long before the forecast error
did, `false_refusal_cost` read **$0.00**, and a shadow-mode report run at production caps would
have pronounced the engine healthy. The bug was **latent, not absent** — lifting the cap on the
same household, the same day, the same realized future, priced it at **$867.13** immediately.

Two things have changed and one has not.

The cap is now **$1,600**, raised to meet the weekly cap when the cadence went weekly ([9]).
So it no longer binds first, and the forecast error is no longer hidden: `false_refusal_cost`
across the population reads ~**$544K**, not zero ([6.1]). The masking that made this section
urgent is not currently in effect.

**But `calibrate.py` sweeps the spend dial, not the cap.** The requirement this section states —
*the harness must sweep the cap across a range* — remains **unmet**. It happens not to bite today
because the shipped cap is generous, which is exactly the sort of accident that stops being true
the moment someone tunes a policy default. And [`prd.md`](./prd.md) §8's *"raise the ceiling as
calibration proves out"* still cuts the other way: a harness blind to the cap cannot tell you
whether the ceiling is the thing bounding your measured error.

The general rule survives its own example: **a metric measured at one policy setting is a
statement about that setting, not about the engine.**

### [8.4] The bug that justifies the whole harness

`derive_cash_events` emitted **today's** events as future ones. But `Snapshot.accounts[].balance`
is the balance at the *end of today* — everything that happened today is already inside it. So on
a payday the forecast counted that paycheck **twice**: a $2,600 phantom inflow, a projected low
thousands of dollars too high, and a sweep against money that was never there.

It is the exact inverse of [2.3]. Money must arrive **late and small**; counting a paycheck that
has already landed as though it were still coming makes it arrive *twice*.

Across the 60-household population it caused **43 sweep-caused overdrafts** — [`prd.md`](./prd.md)
§5.2's guardrail, the one that outranks the KPI, breached forty-three times.

**The single-household demo reported zero.** Not because the bug was absent, but because whether
it bites depends on the household's cash position on whichever paydays a given seed happens to
produce. One seed simply never landed on one.

That is the entire argument for grading a **population** rather than an anecdote, and it is worth
stating plainly: **a guardrail measured on one household is not measured.** The harness's first
act was to find the thing it was built to find, in the component nobody was looking at.

## [9] How often we are allowed to act

**Sweeps are spaced at least `UserPolicy.min_days_between_sweeps` apart. It ships at 7.**

The engine used to sweep every day, and no document in this repo ever argued for that. It was a
fossil of the v0 advice product, whose output was a *notification* and which listed "daily
engagement" as a feature. [`prd.md`](./prd.md) §0 killed advice-only and kept the rhythm, so a
daily notification cadence quietly became a daily ACH cadence.

It contradicted [`prd.md`](./prd.md) §2 — *"we cannot win this on expected value, we have to win
it on the tail"* — by taking three times as many draws on that tail. And the engine could not
honour it anyway: `SWEEP_IN_FLIGHT` blocks while ACH settles, so each sweep already suppressed
the next several days. The **weekly cap** was doing the pacing, which is not what a cap is for.

Priced across the demo household with the throughput ceiling held constant, daily sweeping is
worth about **$36/yr** in interest timing over weekly. That is its whole economic case, and it is
a real one — the cadence is **not** free.

What buys it back is [`prd.md`](./prd.md) §2.3's guarantee: we reimburse sweep-caused overdraft
fees, so every debit is a draw we pay for when it goes wrong. Daily takes ~142 draws/yr against
weekly's ~45, so daily only pays for itself if the **per-sweep overdraft probability is under
~1%** — and at a realistic all-in cost per incident (fee, missed rent, a lost customer) the
breakeven falls to ~**0.1%**.

**That number is no longer entirely unmeasured — but it is not yet settled either.** [8] has run:
across the population, **0 sweep-caused overdrafts in 590 sweeps**. Zero events does not mean zero
risk; by the rule of three, 0 in 590 puts the **95% upper bound at ~0.51%** per sweep.

Read that carefully, because it lands between the two breakevens and licenses nothing:

- It **rules out** the world where the rate is above ~1% — the fee-only breakeven — so daily is
  not obviously value-destroying.
- It sits **5× above** the ~0.1% all-in breakeven, and cannot exclude it. So daily still cannot
  be shown to pay for itself.
- And it is 590 sweeps against a **synthetic** population whose spend the engine's own forecast
  assumptions generated. It is a bound on the code, not on the world.

**So the cadence remains insurance, priced at ~$36/yr, against a tail we can now bound but not
yet size.** The measurement narrowed the question rather than answering it, which is the honest
outcome and worth saying out loud instead of promoting a bound to a result.

*(The ~$36/yr figure itself predates the payday fix ([8.4]), which moved 41 of the demo
household's 90 projections. It has not been re-derived since, and should be before it is leaned
on again.)*

Full working — including two plugged-in numbers that pointed the right way for the wrong reason, an
interest estimate 5× too high and an ACH-fee claim that was simply wrong —
[`learnings/2026-07-14-the-cadence-was-inherited-not-chosen.md`](./learnings/2026-07-14-the-cadence-was-inherited-not-chosen.md).

### [9.1] The hold runs *after* the forecast, and that placement is the design

`CADENCE_HOLD` is **not** in `_blocking_reasons`, and it must never move there.

A blocking reason short-circuits `decide()` before `conservative_low_balance()` runs, so the
refusal carries no `projected_low_balance` — and `outcome.py:grade()` **raises** on exactly those
([8]), because inventing a projection would feed the calibration distribution zeros that look
like perfect forecasts. Gate the cadence up there and six days in seven stop being gradeable: the
engine would still be wrong daily, it would simply no longer be *measured* daily, and
[`strategy.md`](./strategy.md) §3 says that error distribution is the only asset that compounds.

So the forecast runs every day regardless, and a held day carries its projection with it.

**The cadence limits what we do, never what we know.** Daily data, weekly money.

### [9.2] `false_refusal_cost` on a held day is a deferral, not a loss

The trap this opens, stated before someone falls into it. On a `CADENCE_HOLD` day
`would_sweep()` returns the full surplus while `swept` is zero, so `false_refusal_cost`
([8.2]) reports the whole un-swept amount — but that money is **not gone**. It sits in the
household's checking account and is swept next period. Sum `false_refusal_cost` naively across a
window and you count the same dollars every single day they sit, which is how you would conclude
the cadence costs thousands when it costs tens.

A replay must **partition on `CADENCE_HOLD` before totalling it**. The honest price of the
cadence is the interest on the deferral — the last column of the table in the learnings note, and
about $9 per 90 days for the demo household.

### [9.3] What the spacing rule gets wrong

It is a **spacing** rule, not a **cycle** rule: seven days since the last sweep, wherever that
falls. A household's surplus does not appear on a seven-day rhythm — it appears when they are
paid, and it is spoken for when their bills clear.

The better design decides once per *cash cycle*: the day after income lands and the cycle's known
obligations are covered, which for a semimonthly or biweekly earner is naturally twice a month
and for a monthly earner is once. `sim/household.py` already models pay cadence (`_paydays`,
`_semimonthly`, `_monthly`); the engine does not look at it. A fixed 7-day spacing is a decent
approximation of a biweekly household and a poor one for everyone else.

Not built, deliberately: it needs the recurring-income detector that [6.2] already lists as
assumed away, and the measured cost of getting it wrong is small ([9]). The dial exists in the
meantime — `min_days_between_sweeps` is a policy value, and `0` restores the daily engine exactly.
