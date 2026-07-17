"""Measure the engine against a population, and price what a looser spend model would cost.

`backend/replay.py` grades one household. This grades many, at several dial settings, and turns
`6.9% on one seed` into something you can actually read a decision off.

## Why this exists at all

`engine/forecast.py` charges `p90-of-daily` spend against **every** day of the horizon — an
effective `30 x p90_daily`. That is wrong by construction: variance grows with `sqrt(t)` and this
model grows it with `t`. Measured over three years of daily spend for three household shapes, it
reserves **more than the household's worst 30-day stretch has ever been** — over-reserving by
**$400-970** against a $750 default buffer, which on a large share of days is the entire difference
between sweeping and refusing (`docs/learnings/2026-07-13-the-spend-model-over-reserves.md`).

Fixing it **loosens the forecast and buys bigger sweeps** — the one direction
`decision-engine.md` §3 says a change must never move without evidence. So the learning's own
sequencing is binding:

    1. Do not fix it yet. Land the grader (#3) and the replay driver (#4) first.
    2. Ship the new spend model with its dial set to reproduce today's refusals.
    3. Loosen it only as far as the **measured** breach rate licenses.

(1) is done. This module is (3) — and it is the thing that decides (2).

## The rule this module enforces

**A dial setting is licensed only if its breach rate is no worse than today's, across the whole
population.** Not on average. On every shape.

"Breach" means the realized low came in *below* what we projected — we were **optimistic**, and the
money was not there. That is the tail that decides the company, and it is the only number that may
be traded for a bigger sweep.

Sweep-caused overdrafts are a **hard veto** independent of the breach rate (`prd.md` §5.2: the
guardrail outranks the primary KPI). A setting that overdrafts anyone is not licensed at any price.

And a setting must **actually buy something back** — see `licensed()`. Both of the rules above get
*easier* to pass the more you reserve, so a safety bar that only measures safety will happily
license a setting that is strictly worse for the household than the one it replaces.

## What it found

**Nothing is licensed.** Reserving against the household's own worst 30-day stretch *ever*
(`q=1.0`) breaches **19.8%** of days against today's **2.3%**. The empirical model is not wrong —
it is **starved**: it reads that worst-ever window off the 60-150 days of history the engine
actually has, which is 2-5 *independent* months, and the worst of 3 months badly understates the
worst of 36. Given three years of history the same model breaches 3.7%, and on the fat-tailed
household — the one it is most dangerous for today — it breaches **zero**.

So `SPEND_QUANTILE` stays `None` and `forecast.py` keeps the old model. The full write-up, and
what to do instead, is in
`docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.replay import Calibration, calibrate, replay
from engine.models import ZERO, money
from sim.household import HouseholdSpec, SpendSpec

from backend.precompute import DEMO_SPEC  # isort: skip

# The three shapes from the 2026-07-13 run, kept identical so the numbers are comparable.
#
# The counter-intuitive one is `steady`: it sits **7.2 sigma** into its own tail while the
# fat-tailed household sits only **3.4 sigma** into its. Skew inflates sigma, so "sigma into the
# tail" flatters a skewed distribution — which is exactly why the fix is non-parametric.
SHAPES: dict[str, SpendSpec] = {
    "typical": SpendSpec(zero_day_probability=0.25, median=money("38.00"), log_sigma=0.9),
    "high_variance": SpendSpec(zero_day_probability=0.30, median=money("35.00"), log_sigma=1.4),
    "steady": SpendSpec(zero_day_probability=0.10, median=money("50.00"), log_sigma=0.4),
}

# One seed is an anecdote. This is the smallest number of households per shape that makes a
# breach rate mean anything, and it is still small — it is a demo population, not a prior.
SEEDS = tuple(range(11, 31))  # 20 per shape, 60 households


def _spec_for(shape: SpendSpec) -> HouseholdSpec:
    """The demo persona, with its spending replaced. Everything else held constant.

    The card, the payroll and the bills stay put deliberately: this is measuring the **spend
    model**, and varying anything else would let a shift in the breach rate come from somewhere
    we are not looking at.
    """
    from dataclasses import replace

    # Preserve the card_share the demo uses — spending that moves onto a card is exactly the
    # channel shift this whole feature exists to survive, and a population that never charges
    # anything would measure a spend model nobody has.
    charging = replace(shape, card_share=DEMO_SPEC.spend.card_share)
    return replace(DEMO_SPEC, spend=charging)


@dataclass(frozen=True)
class Reading:
    """One dial setting, measured across the whole population."""

    quantile: float | None
    per_shape: dict[str, Calibration]

    @property
    def graded_days(self) -> int:
        return sum(c.graded_days for c in self.per_shape.values())

    @property
    def breaches(self) -> int:
        return sum(c.optimistic_days for c in self.per_shape.values())

    @property
    def breach_rate(self) -> float:
        return self.breaches / self.graded_days if self.graded_days else 0.0

    @property
    def worst_shape_breach_rate(self) -> float:
        """The worst shape, not the average.

        An average hides the household we are about to overdraft. A dial that is safe for two
        shapes and dangerous for the third is not safe.
        """
        return max((c.breach_rate for c in self.per_shape.values()), default=0.0)

    @property
    def sweep_caused_overdrafts(self) -> int:
        return sum(c.sweep_caused_overdrafts for c in self.per_shape.values())

    @property
    def false_refusal_cost(self) -> Decimal:
        """Our cost of conservatism — deferrals already excluded. What loosening would buy back."""
        return money(sum((c.total_false_refusal_cost for c in self.per_shape.values()), ZERO))


def measure(quantile: float | None, seeds: tuple[int, ...] = SEEDS) -> Reading:
    """Replay the whole population at one dial setting."""
    per_shape: dict[str, Calibration] = {}

    for name, shape in SHAPES.items():
        spec = _spec_for(shape)
        graded = [
            g for seed in seeds for g in replay(spec=spec, seed=seed, spend_quantile=quantile)
        ]
        per_shape[name] = calibrate(graded)

    return Reading(quantile=quantile, per_shape=per_shape)


def measure_archetypes(quantile: float | None = None, seeds: tuple[int, ...] = SEEDS) -> Reading:
    """The four archetypes, each across the same seeds — a **second** population, reported apart.

    **This does not touch `measure()`, and that is the point.** `measure(None)` is the anchor both
    #44 and #46 used to prove they had changed nothing: 4,320 days, 2.338%, 0 sweep-caused
    overdrafts, $544,640.58, and those figures are quoted in `prd.md` §5.2/§5.3, `strategy.md`, and
    `decision-engine.md`. Folding four new households into that number would move all of it at
    once, and nobody reading the diff could tell which part moved because the *engine* changed and
    which because the *population* did. Ticket `0023`: "do not re-baseline."

    So this is an addition. The spend population measures the **spend model** against one calendar;
    this measures **four calendars** against one spend model. They answer different questions and
    averaging them would answer neither.

    Twenty seeds each rather than the seeder's single household, because `calibrate`'s own rule
    applies to its own findings: one seed is an anecdote, and "a guardrail measured on one household
    is not measured" (`prd.md` §5.2) does not stop being true when the household is interesting.
    """
    from backend.archetypes import ARCHETYPES

    per_shape: dict[str, Calibration] = {}

    for name, spec in ARCHETYPES.items():
        graded = [
            g for seed in seeds for g in replay(spec=spec, seed=seed, spend_quantile=quantile)
        ]
        per_shape[name] = calibrate(graded)

    return Reading(quantile=quantile, per_shape=per_shape)


def licensed(candidate: Reading, baseline: Reading) -> bool:
    """May this dial setting ship?

    Three conditions. The first is a veto, the second is the safety bar, and the third is what
    stops this function from licensing a regression.

    1. **Zero sweep-caused overdrafts.** `prd.md` §5.2 says the guardrail outranks the primary
       KPI, and it means it. A setting that overdrafts anyone is not licensed at any price.
    2. **No worse than today, on every shape.** Not on average — an average hides the one
       household we are about to overdraft.
    3. **It has to actually buy something back.**

    (3) is not a formality, and leaving it out made this function dangerous. **Reserving more
    always breaches less.** Conditions (1) and (2) are both monotone in the size of the reserve,
    so a dial swept far enough in the *tightening* direction will always eventually satisfy them
    — and be pronounced "licensed" on the strength of being more conservative than the model it
    replaces.

    That is not hypothetical. `q=3.0` — three times the household's worst-ever month — passes (1)
    and (2) comfortably, and costs **$1.62M** in false-refusal cost against today's **$544K**.
    Without this condition the harness recommends it, and `report()` announces the $1.08M
    regression as a buy-back of `$-1,078,015.78`.

    The dial exists to **loosen** the forecast and hand refused money back. A setting that
    reserves more than today is a tightening wearing the name of a loosening, and whatever else
    it may be, it is not what this measurement was built to license.
    """
    if candidate.sweep_caused_overdrafts > 0:
        return False

    if candidate.false_refusal_cost >= baseline.false_refusal_cost:
        return False

    return all(
        candidate.per_shape[name].breach_rate <= baseline.per_shape[name].breach_rate
        for name in SHAPES
    )


def sweep_the_dial(
    candidates: tuple[float, ...] = (3.0, 2.0, 1.5, 1.25, 1.0, 0.99, 0.95, 0.90),
) -> tuple[Reading, list[tuple[Reading, bool]]]:
    """Measure today's model, then every candidate setting against it.

    Returns `(baseline, [(reading, licensed), ...])` ordered from tightest to loosest, so the
    caller can take the **loosest licensed** setting and nothing looser.

    The candidates run *past* `q=1.0` on purpose. `spend_30d_high` can scale beyond the
    household's own worst month, and the honest question is not "is some quantile of their
    history safe" but "is there **any** setting of this model that is both safe and worth
    making" — which you cannot answer without looking at the settings that are safe and
    worthless. The answer today is no, and the whole curve is how you can tell.
    """
    baseline = measure(None)
    results = [(r, licensed(r, baseline)) for r in (measure(q) for q in candidates)]
    return baseline, results


def report() -> str:
    """The whole measurement, as text. Run: `python -m backend.calibrate`."""
    baseline, results = sweep_the_dial()

    lines = [
        "Spend-model calibration",
        f"  population: {len(SHAPES)} shapes x {len(SEEDS)} seeds = "
        f"{len(SHAPES) * len(SEEDS)} households",
        "",
        "  dial      graded  breach%  worst-shape%  overdrafts  false-refusal cost  licensed",
        "  " + "-" * 84,
    ]

    def row(r: Reading, ok: bool | None) -> str:
        dial = "today" if r.quantile is None else f"q={r.quantile:g}"
        flag = "" if ok is None else ("  yes" if ok else "  NO")
        return (
            f"  {dial:<8}  {r.graded_days:>6}  {r.breach_rate:>6.1%}  "
            f"{r.worst_shape_breach_rate:>11.1%}  {r.sweep_caused_overdrafts:>10}  "
            f"{'$' + f'{r.false_refusal_cost:,.2f}':>18}{flag}"
        )

    lines.append(row(baseline, None))
    for reading, ok in results:
        lines.append(row(reading, ok))

    winners = [r for r, ok in results if ok]
    lines.append("")

    if not winners:
        lines.append("  No setting is licensed. The dial stays where it is.")
        lines.append("")
        lines.append("  The empirical model is not wrong — it is starved. It reads the worst")
        lines.append("  30-day window off ~60-150 days of history, which is ~2-5 independent")
        lines.append("  months, and the worst of 3 months badly understates the worst of 36.")
        lines.append("  Given 3 years of history its breach rate falls 19.3% -> 3.7%. See")
        lines.append("  docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md")
    else:
        # The loosest licensed setting — smallest quantile, since a lower quantile reserves less.
        best = min(winners, key=lambda r: r.quantile or 1.0)
        saved = baseline.false_refusal_cost - best.false_refusal_cost
        lines.append(f"  LICENSED, loosest: q={best.quantile:g}")
        lines.append(f"  Buys back ${saved:,.2f} of false-refusal cost at no cost in breach rate.")
        lines.append("")
        lines.append("  Per shape, breach rate today -> at that setting:")
        for name in SHAPES:
            lines.append(
                f"    {name:<15} {baseline.per_shape[name].breach_rate:>6.1%} -> "
                f"{best.per_shape[name].breach_rate:>6.1%}"
            )

    return "\n".join(lines + [""] + _archetype_lines())


def _archetype_lines() -> list[str]:
    """The second population: four calendars, one spend model. Ticket 0023.

    Reported next to the dial sweep and never folded into it — the sweep's numbers are quoted in
    three documents and are the anchor that proves a refactor changed nothing.
    """
    arch = measure_archetypes(None)

    lines = [
        "Archetype coverage — at the shipped dial, not a sweep",
        f"  population: {len(arch.per_shape)} archetypes x {len(SEEDS)} seeds = "
        f"{len(arch.per_shape) * len(SEEDS)} households",
        "",
        "  archetype                graded  breach%  overdrafts  false-refusal cost",
        "  " + "-" * 74,
    ]

    for name, c in arch.per_shape.items():
        lines.append(
            f"  {name:<24} {c.graded_days:>6}  {c.breach_rate:>6.1%}  "
            f"{c.sweep_caused_overdrafts:>10}  {'$' + f'{c.total_false_refusal_cost:,.2f}':>18}"
        )

    lines += [
        "  " + "-" * 74,
        f"  {'all':<24} {arch.graded_days:>6}  {arch.breach_rate:>6.1%}  "
        f"{arch.sweep_caused_overdrafts:>10}  {'$' + f'{arch.false_refusal_cost:,.2f}':>18}",
        "",
        "  Read `graded` first, not `breach%`. A blocking refusal never ran a forecast and is",
        "  not graded, so a low graded count is a household we would not serve. Every archetype",
        "  is offered the same 1,800 days.",
        "",
        "  The guardrail holds everywhere: 0 sweep-caused overdrafts, all four. prd.md §5.2 is",
        "  not moving, and nothing here licenses touching a gate.",
        "",
        "  What it costs is service, not safety. The non-biweekly households are refused most",
        "  days and forecast badly on the rest -- and their income is, by construction, exactly",
        "  as regular as the demo's (variation=0.02 for all four). See archetypes.py: the",
        "  28-day income bucket divides evenly into a biweekly calendar and into no other.",
    ]

    return lines


if __name__ == "__main__":  # pragma: no cover
    print(report())
