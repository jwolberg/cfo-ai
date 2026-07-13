# The spend model reserves more than the household has ever spent

**Date:** 2026-07-13 · **Found by:** ticket #1's falsification run · **Status:** confirmed,
not yet fixed · **Tests:** `tests/test_spend_model.py`

## The claim, and how it was tested

`engine/forecast.py:100` subtracts `daily_discretionary_high` — documented as a **p90 of
DAILY spend** — on every one of the 30 horizon days.

I asserted early in this session that compounding a per-day quantile over 30 days lands
roughly 7σ into the tail of the distribution that actually matters, and that it was therefore
the binding constraint on nearly every decision the engine makes. That was arithmetic on a
napkin, so ticket #1's generator was built partly to **kill it**.

It survived.

## What the measurement says

Three years of daily spend per household (1,066 overlapping 30-day windows), for three
household shapes. "Engine assumes" is `30 × p90_daily`. "Real" is the household's own
enumerated 30-day windows.

| Household | Engine assumes | Real p99 | **Worst ever seen** | σ into the tail | Over-reserved vs p99 |
|---|---|---|---|---|---|
| Typical (lognormal σ=0.9) | $3,101.70 | $2,128.34 | $2,231.26 | 5.7 | **$973.36** |
| High-variance (σ=1.4, fat tail) | $4,461.00 | $4,058.78 | $4,439.43 | 3.4 | **$402.22** |
| Steady (σ=0.4, near-Gaussian) | $2,403.30 | $1,785.33 | $1,870.57 | 7.2 | **$617.97** |

**In all three, the engine reserves more than the household's worst 30-day stretch in three
years.** Not a conservative estimate of a bad month — a month worse than any they have ever
had.

The cleanest single statement of the bug: **variance grows with √t, and this model grows it
with t.** A 30-day sum of daily spend has 30× the mean but only √30 ≈ 5.5× the standard
deviation. Applying the daily quantile's *whole* deviation on all 30 days inflates the padding
by about 5.5×.

## Why it matters, and why it would have survived indefinitely

The over-reservation is **$400–$970**. The default `buffer_floor` is $750. So on a large share
of days this single modelling error is the entire difference between sweeping and refusing.

And it fails in the **safe** direction — nobody is overdrawn, nothing throws, no alert fires.
This is exactly the shape `decision-engine.md` §3.1 warns about: the product simply refuses
more often than it should, quietly, forever, and the metric that would eventually reveal it is
a disappointing revenue curve two years later. It is `false_refusal_cost` (ticket #3), and it
is why that metric has to exist.

## The counter-intuitive detail worth keeping

The **σ count is lowest for the fat-tailed household** (3.4σ) and highest for the steady one
(7.2σ). Skew inflates the standard deviation, so "σ into the tail" flatters a skewed
distribution. This is precisely why the fix should **not** be a parametric `μ + zσ√t`: real
spend is zero-inflated and right-skewed, and σ is a poor description of its tail.

The better instrument is the one this run used: **enumerate the household's own historical
windows and read the quantile off them directly.** Non-parametric, no distributional
assumption, uses their real skew and autocorrelation, and it is explainable in a sentence —
*"your worst 30-day stretch last year was $2,231."*

It is also fully deterministic, which is what lets it live inside an engine that forbids
randomness: the "sampling" is the household's own history, which is already in the `Snapshot`.

## What must NOT happen next

Fixing this **loosens the forecast and buys bigger sweeps**, which is the one direction
`decision-engine.md` §3 says a change must never move without evidence. So:

1. **Do not fix it yet.** Land the grader (#3) and the replay driver (#4) first.
2. Ship the new spend model with its dial set to reproduce today's refusals — no behaviour
   change, no new risk.
3. Loosen it only as far as the **measured** breach rate licenses.

The whole point of the harness is to earn the right to make this change. Making it first would
be trading a measurable, safe error for an unmeasured, unsafe one.
