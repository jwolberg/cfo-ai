# The empirical spend model is not a drop-in — it is starved

*2026-07-14. The sequel to [`the-spend-model-over-reserves`](./2026-07-13-the-spend-model-over-reserves.md),
which proposed this exact fix. We built it, measured it, and it is not safe to ship.*
**Status:** measured, refused, shipped inert · **Code:** `backend/calibrate.py` ·
**Tests:** `tests/test_calibrate.py`, `tests/test_precompute.py::TestTheSpendDial`

## What the last learning told us to do

`30 × p90_daily` over-reserves, because variance grows with `√t` and that model grows it with
`t`. Three years of daily spend confirmed it: the engine reserves **more than the household's
worst 30-day stretch has ever been**. The proposed fix was non-parametric and, on paper,
unimprovable — *enumerate the household's own rolling 30-day windows and read the quantile
straight off them*. No distributional assumption. Their real skew. Their real autocorrelation.
Explainable in one sentence.

It also came with the right sequencing, which we followed: land the grader, land the replay
driver, ship the new model behind a dial set to reproduce today's refusals, and **loosen only as
far as the measured breach rate licenses.**

We built the dial. We measured it. **The breach rate licenses nothing.**

## The measurement

60 households (3 shapes × 20 seeds), 4,320 graded days. "Breach" = the realized low came in
*below* what we projected: we were optimistic, and the money was not there.

| dial | breach | worst shape | sweep-caused overdrafts | false-refusal cost |
|---|---|---|---|---|
| **today** (`30 × p90_daily`) | **2.3%** | 5.8% | 0 | $544,640 |
| `q=1.0` — their worst month *ever* | **19.8%** | 22.0% | 1 | $380,499 |
| `q=0.95` | 22.4% | 24.9% | 1 | $358,037 |
| `q=0.90` | 24.4% | 27.4% | 1 | $317,823 |

Reserving against **the worst 30-day stretch the household has ever actually had** breaches
nearly **nine times** as often as the model we called an over-reserver. It buys back $164K of
false refusals and pays for them with a 2.3% → 19.8% collapse in the only number that decides
the company.

Nothing at or below `q=1.0` is licensed. The dial ships at `None`.

## The explanation that is wrong

The first thing I wrote down — and very nearly committed into a test docstring, where it would
have become institutional knowledge — was that the failure is *structural*: the projected low
lands mid-horizon, and a 30-day total amortized flat has only charged a fraction of itself by
the time the low arrives, so the old model's daily padding was doing work the new model's total
does not do.

It is a good story. It is also arithmetically impossible.

**Both models charge a flat constant against every horizon day.** The old one charges
`p90_daily`; the new one charges `spend_30d_high / 30`. Wherever the low lands — day 4 or day 27
— it scales *both* of them by exactly the same factor. Amortization cannot open a gap between two
models that are both flat. The only thing that differs between them is the size of the constant.

I nearly shipped a plausible mechanism for a real number. The number was real; the mechanism was
decoration.

## The explanation that is right

The model is not wrong. It is **starved**.

`spend_30d_high` reads the worst 30-day window off the history the engine actually has — 60 to
150 days. Those ~90 overlapping windows look like 90 samples, but they are drawn from **two to
five independent months**, and overlapping windows flatter the sample count without adding
information. *The worst of 3 months is a badly biased estimate of the worst of 36.*

Measured against three years of ground truth, at the point in the replay where the engine is
making decisions:

| household | true worst 30-day month (3y) | what the model estimates it as | old `30 × p90` |
|---|---|---|---|
| typical | $2,059 | $1,580 — **77%** | $2,848 — 138% |
| **high-variance** | $4,383 | $2,548 — **58%** | $3,832 — 87% |
| steady | $1,742 | $1,503 — **86%** | $2,324 — 133% |

**The bias is worst exactly where it is most dangerous.** The fat-tailed household — the one
whose bad month is worst and likeliest to arrive — gets a reserve covering **58%** of its true
worst month, because a fat tail means the bad month is *rare*, which means a short history almost
never contains one. The model's error is anti-correlated with safety: it under-reserves most for
the household most likely to blow up.

This is the same trap the 07-13 learning identified in the parametric model, wearing different
clothes. There, σ flattered the skewed household. Here, a short window flatters it. Both
instruments are least trustworthy on the household that needs them most.

## The experiment that proves it

If the cause is starvation, then **feeding the model more history should fix it** — and
amortization, being unchanged by history length, predicts nothing of the kind.

Same households, same seeds, same code. Only the warm-up runway changes (8 seeds/shape, so these
are noisier than the table above; what matters is the movement *within* each block):

| | breach: today | breach: `q=1.0` | overdrafts at `q=1.0` |
|---|---|---|---|
| **60 days of history** (what the engine has) | 3.5% | **19.3%** | 1 |
| **3 years of history** | 2.6% | **3.7%** | 0 |
| ↳ *the fat-tailed household* | 3.6% | **0.0%** | 0 |

Breach falls from **19.3% to 3.7%** on nothing but history. On the high-variance household — the
one it was most dangerous for — the empirical model given three years of history breaches **zero
times**, beating the incumbent outright.

The idea was right. The data was not there.

## The trap in the harness itself

Sweeping the dial *past* `q=1.0` (the model can scale beyond the household's own worst month)
surfaced a bug in the thing doing the licensing.

`licensed()` originally asked two questions: does it overdraft anyone, and is its breach rate no
worse than today's on every shape. **Both are monotone in the size of the reserve — reserving
more always breaches less.** So a dial swept far enough in the *tightening* direction will
eventually satisfy both, and be pronounced licensed for the sole achievement of being more
conservative than the model it replaces.

`q=3.0` does exactly this. It passes both conditions, and it costs **$1.62M** in false-refusal
cost against today's **$544K**. The harness recommends it, and `report()` announces the $1.08M
regression as a buy-back of `$-1,078,015.78`.

A safety bar that only measures safety will always license the least useful thing you show it.
`licensed()` now has a third condition: **it has to actually buy something back.** A setting that
reserves more than today is a tightening wearing the name of a loosening, whatever its breach
rate.

## Where this leaves the spend model

`SPEND_QUANTILE = None`. The structure ships; the loosening does not. `forecast.py` falls back to
`daily_discretionary_high` and the engine behaves today exactly as it did yesterday — pinned by
`TestTheSpendDial`, which fails if anyone moves the dial without a measurement.

The over-reserve documented on 07-13 is **still real and still unfixed**. What we now know is
that the proposed fix is not available *at the history the engine has*, and the honest options
are:

1. **Gate the empirical model on history, not on a quantile.** It is safe — better than the
   incumbent — for households with years of data, and dangerous for the ones we just onboarded.
   A `MIN_HISTORY` for this path of ~60 days is nowhere near enough; the evidence points to
   something north of a year. This is the most promising direction and it is not a dial, it is a
   second model with an eligibility rule.
2. **Scale the empirical estimate to cover the tail it cannot see** — a factor above 1.0 that
   corrects for short-sample bias. Note the correction needed is *shape-dependent* (58% vs 86%),
   so a single global scalar re-introduces exactly the flattering-the-skewed-household problem
   this model was chosen to avoid.
3. **Leave it.** The incumbent is expensive, but it is expensive in the safe direction, and it
   is the only model here that has been measured to overdraft nobody.

What is no longer available is shipping the swap because the reasoning is elegant. It was.

## The one-line version

*A non-parametric estimator is only as good as the tail it has actually seen — and the household
whose tail you most need is the household whose tail you have least seen.*
