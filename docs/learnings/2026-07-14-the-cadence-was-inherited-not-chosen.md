# The sweep cadence was inherited, not chosen — and it did not survive being measured

*2026-07-14. Companion to [`the-spend-model-over-reserves`](./2026-07-13-the-spend-model-over-reserves.md),
and the same shape of bug: an unexamined default, failing quietly, in the direction nobody audits.*

## The claim

The product swept **daily**. No document in this repo ever argued for that. It is a fossil of
the v0 advice product, and when it was finally priced it turned out to buy the household
roughly **$36 a year** — about what it costs to send the extra ACH debits it requires.

## Where it came from

`archive/prd-v0-advice-only.md` describes an *advice* product. It "answers one question every
day," and lists **"Daily engagement"** (line 65) among the reasons the problem was attractive —
daily was an engagement property, chosen for a product whose entire output was a notification.

[`prd.md`](../prd.md) §0 then killed advice-only on the RCT evidence, correctly and at length.
But it kept the rhythm and swapped the payload. §1's headline became:

> **Every day, we move the cash you do not need onto the debt that costs you the most.**

A daily *notification* cadence had silently become a daily *ACH* cadence, and nothing in the
PRD, the plan, [`decision-engine.md`](../decision-engine.md), or `decision-flow.html` ever
revisited it. Everything downstream simply inherited it — including `decision-engine.md` §7,
which justified modelling interest daily "**because the product sweeps daily**." That is the
interest model deferring to the cadence, not evidence for it.

## Why it should have been caught earlier

The PRD contradicts itself on this, in its own strongest section. §2:

> Every sweep is a draw from a probability distribution. At scale, some non-zero fraction will
> overdraft someone... **We cannot win this on expected value. We have to win it on the tail.**

Then it takes ~12 draws a month instead of ~4. And §1.1 says what the product sells is **"the
absence of a decision"** — while the design made 30 of them a month and notified the user about
a dozen.

**The engine could not do it anyway.** `decide()` refuses on `SWEEP_IN_FLIGHT` while a sweep is
unsettled, and ACH settles in 2–3 business days, so each sweep already blocked the next several
days. Daily sweeping was never reachable in production. The demo did not show this only because
`backend/precompute.py` hardcodes `sweeps_in_flight=ZERO` — which is why the old artifact opened
on runs of four consecutive $400 sweeps that no real household could ever have received. What
was actually pacing the engine was the **weekly cap**: a governor bolted on to blunt a frequency
nobody had chosen.

## What it was worth — measured, not asserted

The one honest way to settle it is [6.1]'s rule: run it, don't argue about it. Holding the
throughput ceiling constant at $1,600/week so that **cadence is the only variable** (scale the
per-sweep cap with the spacing, or a starved cap wears the cadence's costume — the same trap
[8.3] describes for the replay driver), across the demo household's 90-day window:

| Spacing | ACH/month | Mean sweep | Sweeps < $100 | End debt | End checking | **Net cost to the household** |
|---|---|---|---|---|---|---|
| Daily | 11.7 | $255 | 9 | $3,452 | $3,847 | — |
| **7 days** | **3.7** | **$645** | **1** | $4,012 | $4,398 | **$9 / 90d (~$36/yr)** |
| 14 days | 2.0 | $1,084 | 1 | $3,809 | $4,182 | $23 / 90d (~$92/yr) |
| 30 days | 1.0 | $2,304 | 0 | $5,359 | $5,698 | $57 / 90d (~$227/yr) |

**Read the last two columns together or you will get this wrong.** A slower cadence ends the
window with more debt — but with almost exactly the same amount of *extra cash still sitting in
checking*. The money is not lost, it is **deferred**: it gets swept next period. The household's
actual cost is the interest on the deferral, which is the final column, and it is small.

This is worth dwelling on, because the first estimate made in this investigation — a
back-of-envelope that priced each deferred dollar for the full length of its delay — came out
around **$200/yr for a fortnightly cadence, roughly 5× the truth**. It double-counted: the same
dollars are swept the following week, so only the marginal delay costs anything. An estimate
made *before* the measurement was wrong in the direction that flattered the status quo. The
repo's own rule ([6.1], [8.3]) exists precisely for this, and it caught its author.

### What the $36/yr has to be weighed against — and a claim I got wrong

**First draft of this note said daily also costs "~$48/yr more in ACH fees," at ~$0.50 a debit.
That number was assumed, not sourced, and it is mostly wrong.** It is left here rather than
quietly deleted, because the mistake is instructive: it is the *second* time in this
investigation that a plugged-in figure pointed the right way for the wrong reason.

Per-transaction ACH cost depends on the **shape** of the processor's pricing, not just its level,
and only one of the three common shapes rewards batching at all:

| Pricing shape | Example | Does spacing sweeps out save fees? |
|---|---|---|
| Flat per transaction | $0.25–$1.00/debit | **Yes** — you pay per transfer |
| Percentage of amount | [Dwolla pay-as-you-go: 0.5%, capped at $5](https://www.dwolla.com/pricing) | **Barely** — you pay per *dollar*. Only the cap helps: one $1,600 sweep costs $5 (0.31%); four $400 sweeps cost $8 (0.5%). |
| Flat monthly plan | Dwolla plans from ~$250/mo | **No** — marginal cost per transfer is zero |

The network itself is a rounding error (FedACH/NACHA per-item fees are fractions of a cent).

**What genuinely scales with the number of debits, under every pricing shape, is returns.**
[Return fees run $2–$5 and up to $20+, and the receiving bank adds an NSF fee of $15–$35](https://stripe.com/resources/more/ach-returns-101-what-they-are-and-how-to-manage-them).
More seriously, [NACHA polices return *rates* on a rolling 60-day window](https://www.nacha.org/rules/ach-network-risk-and-enforcement-topics)
— 0.5% unauthorized, 3% administrative, 15% overall — and breaching them puts the ability to
originate ACH at all on the table. That is an existential per-transaction risk that does not care
how the processor bills.

### So the honest case for weekly is insurance, not a free lunch

Strip out the bogus fee claim and **daily has a small positive economic edge**: the measured
$36/yr. What offsets it is [`prd.md`](../prd.md) §2.3's guarantee — we reimburse sweep-caused
overdraft fees, so every debit is a draw we pay for when it goes wrong.

Daily takes ~142 draws/yr; weekly takes ~45. Setting $36/yr against 97 extra draws at $35 a
reimbursement, **daily pays for itself only if the per-sweep overdraft probability is under
~1%.** Nobody has measured that probability — it is exactly what §8's shadow mode exists to
produce. And §2 says not to make this bet on expected value at all, because the true cost of an
incident is not $35: it is "a $35 fee, a missed rent payment, and a permanently lost customer who
tells everyone." At a few hundred dollars all-in, the breakeven falls to ~**0.1%**, a bar the
engine has never demonstrated it clears.

**The trade is therefore: ~$36/yr of the household's money to take a third as many draws on the
tail, until calibration tells us what the tail is.** That is cheap insurance and it is the same
posture the repo takes everywhere else. It is not the free lunch the first draft claimed.

Nine of the daily engine's 35 sweeps were under $100; the smallest was **$23.19**. `MIN_SWEEP`
is still **$1.00**, so the engine remains licensed to originate a one-dollar ACH — worth
essentially nothing under any pricing shape, and still a draw.

## What changed

`UserPolicy.min_days_between_sweeps` (default **7**), `Snapshot.days_since_last_sweep`, and a new
`ReasonCode.CADENCE_HOLD`. The demo policy's per-sweep cap rose from $400 to $1,600 to meet the
weekly cap: **the ceiling on money moved is unchanged at $1,600/week** — what changed is that it
now takes one ACH debit to reach it instead of four. Leaving the cap at $400 would have cut the
household's throughput 4× and disguised a cadence change as a paydown regression.

It is a **policy value, not a law**. `0` restores the daily engine exactly; `30` gives a monthly
one. The table above is what the dial costs at each setting.

### The one design decision worth remembering

**`CADENCE_HOLD` is not a blocking gate.** It is raised *after* the forecast runs, and the
refusal carries the projection with it.

Put it in `_blocking_reasons` and `decide()` short-circuits before `conservative_low_balance()`,
so the refusal has no `projected_low_balance` — and `engine/outcome.py:grade()` **raises** on
those, on the grounds that inventing a projection would feed the calibration distribution zeros
that look like perfect forecasts. Six days in seven would have dropped out of the record. The
engine would still have been wrong daily; it would simply no longer have been *measured* daily,
and [`strategy.md`](../strategy.md) §3 says that error distribution is the only asset that
compounds.

**The cadence limits what we do, never what we know.** Daily data, weekly money.

## Still open

- **`MIN_SWEEP = $1.00`** is indefensible at any cadence and was left alone deliberately — it is
  its own money-rule change with its own tradeoff, and bundling it here would have hidden it.
- **`sweeps_in_flight=ZERO` in the demo** is now the last thing overstating the achievable
  cadence. With a real settlement model the effective spacing is longer than 7 days anyway.
- **`WEEKLY_CAP` is no longer reachable at the shipped policy** — one sweep a week has nothing to
  stack against. Not dead code (a 3-day spacing with a high per-sweep cap still reaches it), but
  it is now a backstop rather than the thing actually paying the household's cadence.
- **The cadence is a spacing rule, not a cycle rule.** The better design anchors it to the
  household's *own* cash cycle — decide the day after income lands and the cycle's bills are
  known, which for a semimonthly earner is naturally twice a month. `sim/household.py` already
  models pay cadence (`_paydays`, `_semimonthly`, `_monthly`); the engine does not use it. A
  fixed 7-day spacing is a decent approximation of a biweekly household and a poor one for
  everybody else.
- **`false_refusal_cost` on a hold day is a deferral, not a loss.** It reports the full un-swept
  amount, and that money moves next week. Summing it across held days would count the same
  dollars every day they sit. A replay must partition on `CADENCE_HOLD` before totalling it —
  see [`decision-engine.md`](../decision-engine.md) §9.2.
