"""The audit trail must never disagree with the engine it audits.

`backend/trace.py` mirrors `decide()`'s gate order by hand so it can narrate the path. That hand-
mirroring is exactly what rots: reorder a gate in `decide()`, forget this file, and the trail keeps
telling a confident story about a flow that no longer runs. These tests make that impossible to ship
quietly — the trace's terminal gate must land on the same action `decide()` actually returned, for
**every** day of the demo window, not just the two the panel features.
"""

from __future__ import annotations

from datetime import date

from backend.precompute import (
    DEMO_POLICY,
    DEMO_SPEC,
    SEED,
    SERVED_DAYS,
    SPEND_QUANTILE,
    WARMUP_DAYS,
    WINDOW_START,
    generate,
    walk,
)
from backend.trace import BLOCK, trace
from engine.decide import decide
from engine.models import Action


def _walk_days():
    total = WARMUP_DAYS + SERVED_DAYS
    history = generate(DEMO_SPEC, start=WINDOW_START, days=total, seed=SEED)
    return list(walk(history, DEMO_SPEC, WINDOW_START, total, DEMO_POLICY, SPEND_QUANTILE))


WALK = _walk_days()
SERVED = [w for w in WALK if w.offset >= WARMUP_DAYS]


class TestTheTraceMatchesTheEngine:
    def test_headline_is_decides_own_output_every_served_day(self) -> None:
        """The trace reports `decide()`'s action/amount/target verbatim — no reconstruction."""
        for w in SERVED:
            t = trace(w.snapshot)
            d = decide(w.snapshot)
            assert (t.action, t.amount, t.target) == (
                d.action.value,
                d.amount,
                d.target_debt_id,
            ), f"trace headline diverged from decide() on {w.day}"

    def test_terminal_gate_agrees_with_the_action(self) -> None:
        """A refusal ends on a BLOCK gate; a sweep ends on the Decision step. The last step is
        always the terminal one, so the flow the panel draws matches the flow that ran."""
        for w in SERVED:
            t = trace(w.snapshot)
            last = t.steps[-1]
            assert last.terminal, f"trace did not terminate cleanly on {w.day}"
            if w.decision.action is Action.REFUSE:
                assert last.result == BLOCK, f"refusal on {w.day} did not end on a blocking gate"
            else:
                assert last.stage == "Decision", (
                    f"sweep on {w.day} did not end on the Decision step"
                )

    def test_exactly_one_terminal_step(self) -> None:
        """The trace stops at the first gate that decides — no steps recorded past the verdict."""
        for w in SERVED:
            t = trace(w.snapshot)
            assert sum(1 for s in t.steps if s.terminal) == 1, f"multiple terminal steps on {w.day}"


class TestTheTwoHeroDays:
    """The days the CEO panel features. Pin the exact arithmetic so a reseed that changes them is
    caught here, not on screen."""

    def test_may_25_is_the_sweep_with_the_transparent_subtraction(self) -> None:
        w = next(w for w in SERVED if w.day == date(2026, 5, 25))
        t = trace(w.snapshot)
        assert (t.action, t.amount, t.target) == ("sweep", w.decision.amount, "card_demo")
        surplus = next(s for s in t.steps if s.stage == "Surplus")
        assert surplus.inputs["= available"] == "$449.50"
        assert surplus.inputs["projected low"] == "$1,699.50"

    def test_may_30_refuses_despite_real_surplus(self) -> None:
        """The load-bearing contrast: surplus is positive, cadence still holds it."""
        w = next(w for w in SERVED if w.day == date(2026, 5, 30))
        t = trace(w.snapshot)
        assert t.action == "refuse"
        surplus = next(s for s in t.steps if s.stage == "Surplus")
        assert surplus.inputs["= available"] == "$41.47"  # positive — money was available
        cadence = next(s for s in t.steps if s.stage == "Cadence")
        assert cadence.result == BLOCK and cadence.terminal
