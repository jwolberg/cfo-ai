"""The spend surface — per household, and per card. Ticket 0031.

`/spend` was the last route reading `backend/data/decisions.json`, the last route serving a single
household, and the last home of the `cards[0]` defect that `0027` removed from the walk and `0030`
removed from the artifact. This module is where all three end.

## The surface splits, and the split is not a compromise

`0031` framed its first problem as a choice — store a derived projection, or wait for the
`transactions` table ingest has not built (`architecture.md` [3.1]). The code says the surface
divides, and the line falls exactly where the system of record already ends:

| | source | needs ingest? |
|---|---|---|
| statement, unbilled, **held_back** | the stored `Snapshot` | **no** |
| charged/paid last cycle, the rolling 30-day series | the transaction `History` | **yes** |

So the first half is derived **live** from rows Postgres already holds, for every household, on
every request — and is never stored a second time. Storing it would duplicate what the snapshot
holds and let the two disagree about what the engine saw, which is the argument
`backend/readpath.py` already makes against denormalizing display fields onto `decisions`, and
which `architecture.md` [4.1] is a whole section about.

Only the second half is a `SpendProjection`: written by the seeder, read back here, and **deleted
by ingest**. It is the smallest thing that cannot be derived, and it is stored knowingly rather
than quietly — which is what `0031`'s "out of scope" note asked for.

## `held_back` is per card, and that is exact

`0031` calls the reserve "a portfolio-level fact, because `untouchable()` reserves against every
card at once", and treats deciding per-card-vs-aggregate as most of the ticket. `untouchable()` is:

    reserved = sum(obligation_in_horizon(card, horizon_end) for card in portfolio.cards)

A sum over cards **decomposes into its terms**. Each card's `held_back` is its own
`obligation_in_horizon` — an attribution, not an allocation, and not a guess. The portfolio total
is the sum, which is the number `decide.py` actually withheld. Both are true at once, so both are
reported.

That the reserve reads per card is what makes the per-card shape honest rather than decorative:
two cards' statements fall due weeks apart, and `SpendSnapshot`'s original argument — the two
obligations are reported separately because they fall due a **month apart**, and a single "what you
owe" figure hides exactly the thing the user needs to see — does not weaken when there are three
cards. It is the reason there is no single due date to print.

## What is not here, and must not be

`cards.observed_monthly_charges` and `observed_monthly_payment` sit in the `cards` table and look
like they would spare us the projection entirely. They would not: they are **trailing engine
inputs** with a different definition (`precompute.observed_monthly_*` averages over a window;
`charged_last_cycle` is the exact `[close, close]` cycle). Serving them as "what your card took
last cycle" would be a different number wearing the right label — the same shape as every defect
this ticket set has found, and it would never have shown a symptom.

**Comprehension, not a decision.** Nothing served here feeds `forecast.py`. The rolling series is
the structure that will eventually replace `daily_discretionary_high`, rendered a release *before*
it is trusted with a decision; swapping the forecast onto it today would **loosen** the reserve,
and loosening needs a measured breach rate `engine/outcome.py` cannot yet produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from engine.decide import obligation_in_horizon
from engine.forecast import HORIZON_DAYS
from engine.models import ZERO, Snapshot


class SpendSurfaceError(RuntimeError):
    """The surface could not be assembled. Raised at the boundary, never served half-built."""


# --- the projection: what only the transaction History can say -----------------------


@dataclass(frozen=True)
class CardCycleTotals:
    """What one card took last cycle, against what came off it.

    Per card, because "the card grew" is a fact about a card. A portfolio household with a
    transactor and a revolver has one of each, and summing them into a single "your cards grew by"
    would report a household in trouble as a household doing fine.
    """

    card_id: str
    charged_last_cycle: Decimal
    paid_last_cycle: Decimal

    @property
    def grew_by(self) -> Decimal:
        """Positive means the card **grew**. A sweep is not what fixes that."""
        return self.charged_last_cycle - self.paid_last_cycle


@dataclass(frozen=True)
class SpendProjection:
    """The History-derived half of the surface. **A projection, and ingest deletes it.**

    Everything here is derived from the full transaction `History`, which this service does not
    store: there is no `transactions` table until ingest lands (`architecture.md` [3.1]). The
    seeder holds the `History` it walked, so it derives this once and writes it.

    This is the honest cost of shipping the surface before ingest, and it is deliberately the
    *smallest* such cost: the obligations half is not here, because it can be derived from the
    snapshot on every request and a stored copy of a derivable fact is a copy that can disagree.
    """

    as_of: date
    # Every overlapping 30-day total in the trailing window, by channel. The strip chart.
    # Cash is a household fact; the card series is portfolio-wide (every card's charges), which
    # is what `derive_spend_profile` has always summed and what "what does a bad month look like"
    # actually means for a household holding three cards.
    rolling_30d_cash: tuple[Decimal, ...]
    rolling_30d_card: tuple[Decimal, ...]
    last_cycle: tuple[CardCycleTotals, ...]

    @property
    def worst_30d_cash(self) -> Decimal:
        return max(self.rolling_30d_cash, default=ZERO)

    @property
    def worst_30d_card(self) -> Decimal:
        return max(self.rolling_30d_card, default=ZERO)

    def totals_for(self, card_id: str) -> CardCycleTotals | None:
        for totals in self.last_cycle:
            if totals.card_id == card_id:
                return totals
        return None


# --- the obligations: derived live, from the snapshot the engine saw ------------------


@dataclass(frozen=True)
class CardObligations:
    """One card's two obligations, and what the engine held back for them.

    `held_back` is this card's own `obligation_in_horizon` — the exact term it contributes to
    `untouchable()`'s sum, not a share of the total apportioned after the fact.
    """

    card_id: str
    statement_balance: Decimal
    statement_due: date
    unbilled_balance: Decimal
    unbilled_due: date
    held_back: Decimal


@dataclass(frozen=True)
class SpendSurface:
    """What the Spending screen renders, for a household with any number of cards."""

    as_of: date
    cards: tuple[CardObligations, ...]
    projection: SpendProjection

    @property
    def statement_total(self) -> Decimal:
        return sum((c.statement_balance for c in self.cards), ZERO)

    @property
    def unbilled_total(self) -> Decimal:
        return sum((c.unbilled_balance for c in self.cards), ZERO)

    @property
    def held_back_total(self) -> Decimal:
        """The portfolio reserve. Identical to `untouchable(snapshot)[1]` by construction —
        the same sum over the same terms, which is why the per-card figures can be trusted to
        add up to the number the engine actually withheld."""
        return sum((c.held_back for c in self.cards), ZERO)


def derive_obligations(snapshot: Snapshot) -> tuple[CardObligations, ...]:
    """Every card's obligations, as the engine saw them on `snapshot.today`.

    **The horizon is `decide.py`'s, not a second copy of it.** `obligation_in_horizon` is imported
    rather than reimplemented for the same reason `engine/outcome.py` imports `untouchable()`: two
    copies of this arithmetic would drift, and the day they drifted the dashboard would start
    explaining a reserve the engine never took.
    """
    horizon_end = snapshot.today + timedelta(days=HORIZON_DAYS)

    return tuple(
        CardObligations(
            card_id=card.card_id,
            statement_balance=card.statement_balance,
            statement_due=card.statement_due_date,
            unbilled_balance=card.unbilled_balance,
            unbilled_due=card.cycle.due_for(card.next_close_date),
            held_back=obligation_in_horizon(card, horizon_end),
        )
        for card in snapshot.portfolio.cards
    )


def assemble(snapshot: Snapshot, projection: SpendProjection) -> SpendSurface:
    """The two halves, joined on the day they both describe.

    **The day must match, and a mismatch raises rather than renders.** The projection is written by
    the seeder alongside the decisions and describes the last day served; the obligations come from
    that same day's snapshot. If they disagree, one of the two is stale — a re-seed that wrote
    decisions and not the projection — and the screen would show this month's statement beside last
    month's spending without a hint that it had done so. That is the "two copies quietly disagree"
    failure this module's docstring exists to avoid, so it fails loudly instead.
    """
    if projection.as_of != snapshot.today:
        raise SpendSurfaceError(
            f"the projection describes {projection.as_of} and the snapshot {snapshot.today} — "
            "one of them is stale; re-seed the household"
        )

    return SpendSurface(
        as_of=snapshot.today,
        cards=derive_obligations(snapshot),
        projection=projection,
    )
