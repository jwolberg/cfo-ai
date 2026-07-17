"""The spend surface's two halves, and the seam between them. Ticket `0031`.

`backend/spend.py` joins something derived **live** from the stored `Snapshot` (each card's
obligations, and the reserve held against it) to something **stored** because only the transaction
`History` can answer it (the rolling series, and what each card took last cycle).

The tests that matter here are about that seam:

- **The reserve decomposes exactly.** `held_back` per card must sum to `untouchable()`'s figure —
  the number the engine actually withheld — because the whole per-card shape rests on that being an
  attribution rather than an allocation. If it ever stops holding, the dashboard is explaining a
  reserve nobody took.
- **The halves cannot silently describe different days.** A projection from one day beside a
  snapshot from another renders this month's statement next to last month's spending, with nothing
  on screen saying so.

No database needed: this is the pure layer. `tests/test_backend_api.py` drives the same code
through the route, against Postgres.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.archetypes import ARCHETYPES
from backend.precompute import (
    SEED,
    SERVED_DAYS,
    WARMUP_DAYS,
    WINDOW_START,
    derive_spend_projection,
    walk,
)
from backend.spend import SpendSurfaceError, assemble, derive_obligations
from engine.decide import untouchable
from sim.household import HouseholdSpec, generate


def _last_day(spec: HouseholdSpec):
    """The history and the final `WalkDay` — the day a surface describes."""
    days = WARMUP_DAYS + SERVED_DAYS
    history = generate(spec, start=WINDOW_START, days=days, seed=SEED)

    final = None
    for w in walk(history, spec, WINDOW_START, days):
        final = w

    assert final is not None
    return history, final


def _surface(name: str):
    history, w = _last_day(ARCHETYPES[name])
    projection = derive_spend_projection(history, w.day, w.snapshot.portfolio)
    return w.snapshot, assemble(w.snapshot, projection)


ARCHETYPE_NAMES = list(ARCHETYPES)


class TestTheReserveDecomposes:
    """The claim the per-card shape stands on.

    `0031` called `held_back` "a portfolio-level fact, because `untouchable()` reserves against
    every card at once". It is a **sum over the cards**, and a sum decomposes into its terms.
    """

    @pytest.mark.parametrize("name", ARCHETYPE_NAMES)
    def test_the_per_card_reserve_sums_to_what_the_engine_withheld(self, name: str) -> None:
        """Against `untouchable()` itself, not against a re-derivation of it.

        This is the assertion that makes the per-card `held_back` figures trustworthy: they are the
        actual terms of the actual sum `decide.py` took, for every archetype including the two the
        old `cards[0]` code could never have reported.
        """
        snapshot, surface = _surface(name)

        _, reserved = untouchable(snapshot)

        assert surface.held_back_total == reserved

    @pytest.mark.parametrize("name", ARCHETYPE_NAMES)
    def test_every_card_is_described(self, name: str) -> None:
        snapshot, surface = _surface(name)

        assert {c.card_id for c in surface.cards} == {c.card_id for c in snapshot.portfolio.cards}

    def test_a_portfolios_cards_do_not_share_a_due_date(self) -> None:
        """Why there is no single "what you owe", and no `totals.due`.

        The month-apart argument does not weaken with three cards — it is the reason a combined
        due date would have to pick one card's date and be wrong about the others.
        """
        _, surface = _surface("semimonthly_portfolio")

        assert len(surface.cards) == 3
        assert len({c.statement_due for c in surface.cards}) > 1

    def test_the_totals_are_sums_of_their_cards(self) -> None:
        _, surface = _surface("semimonthly_portfolio")

        assert surface.statement_total == sum(c.statement_balance for c in surface.cards)
        assert surface.unbilled_total == sum(c.unbilled_balance for c in surface.cards)


class TestTheHalvesMustDescribeTheSameDay:
    """The seam's one guard, and it is here because nothing else would exercise it.

    The seeder writes the projection in the same transaction as the decisions, so the days agree by
    construction — which is exactly the kind of mechanism this repo keeps finding built, tested by
    nothing, and quietly broken later. A re-seed that wrote decisions and skipped the projection is
    the real path to it.
    """

    def test_a_stale_projection_raises_rather_than_renders(self) -> None:
        history, w = _last_day(ARCHETYPES["demo_biweekly"])
        projection = derive_spend_projection(history, w.day, w.snapshot.portfolio)

        stale = replace(projection, as_of=projection.as_of - timedelta(days=31))

        with pytest.raises(SpendSurfaceError, match="stale"):
            assemble(w.snapshot, stale)

    def test_the_matching_day_is_what_makes_it_pass(self) -> None:
        """The other side of the mutation: same code, same snapshot, correct day — no raise.

        Without this the test above would pass against a function that raised unconditionally.
        """
        history, w = _last_day(ARCHETYPES["demo_biweekly"])
        projection = derive_spend_projection(history, w.day, w.snapshot.portfolio)

        surface = assemble(w.snapshot, projection)

        assert surface.as_of == w.day == projection.as_of


class TestTheObligationsComeFromTheSnapshot:
    """The half that needed no ingest, which is `0031`'s finding."""

    def test_they_need_nothing_but_a_snapshot(self) -> None:
        """No `History`, no projection, no database — the argument for deriving them live.

        `derive_obligations` takes exactly one argument, and that argument is a row Postgres has
        been holding since `0022`. That is the whole reason the "this cycle" panel did not have to
        wait for the `transactions` table.
        """
        _, w = _last_day(ARCHETYPES["semimonthly_portfolio"])

        cards = derive_obligations(w.snapshot)

        assert len(cards) == 3
        assert all(c.unbilled_due > c.statement_due for c in cards)

    def test_the_unbilled_statement_is_due_after_the_closed_one(self) -> None:
        """They are a month apart, and that gap is the whole reason card spend is an engine input:
        two charges three weeks apart leave checking a month apart.

        Inherited from `TestTheSpendSnapshot`, which asserted it of `build().spend` — now true of
        every card of every archetype rather than of `cards[0]` of one.
        """
        for name in ARCHETYPE_NAMES:
            _, surface = _surface(name)
            for card in surface.cards:
                assert card.unbilled_due > card.statement_due, f"{name}/{card.card_id}"
