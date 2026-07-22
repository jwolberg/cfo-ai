"""What the household picker calls each household.

Spawned 2026-07-22 from running the demo against production: the picker showed a reviewer the raw
string `hh_demo_plaid` sitting between three prose labels. Nothing was broken underneath — the
household served a real live decision — but the switcher is the first surface a reviewer touches,
and `USERS.md` §2 says the demonstration **is** the product surface.

Nothing pinned this, which is why it shipped. The label is copy, and copy that only exists in
production is copy nobody notices is wrong until someone is watching.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from backend import readpath


class TestTheLabelCopy:
    """`Household.label` is pure, so these need no database — they are about the words."""

    def test_a_known_archetype_reads_as_prose(self) -> None:
        assert readpath.Household("hh_demo_biweekly", "demo_biweekly").label == "Biweekly, one card"

    def test_the_imported_plaid_household_is_named_not_spelled(self) -> None:
        """The regression. A demo household with no archetype used to fall through to its id.

        The null is load-bearing — `mobile/src/screens/Dashboard.tsx` keys off `archetype === null`
        to take the live-decision path — so this cannot be fixed by inventing an archetype.
        """
        household = readpath.Household("hh_demo_plaid", None, is_demo=True)
        assert household.label == "Live Plaid data"
        assert household.id not in household.label

    def test_a_real_household_is_still_not_named_by_the_demo(self) -> None:
        """`null` archetype and **not** a demo household means a real customer's household. The
        demo does not get to name that one, so the id stands — see the `label` docstring."""
        assert readpath.Household("hh_a1b2c3", None, is_demo=False).label == "hh_a1b2c3"

    def test_an_unknown_archetype_falls_back_to_the_archetype(self) -> None:
        """A seeded archetype nobody wrote copy for shows the archetype, not the id — it is at
        least the thing that distinguishes the household."""
        assert readpath.Household("hh_x", "brand_new_archetype").label == "brand_new_archetype"


class TestTheLabelSurvivesTheQuery:
    """The copy above is only true if `list_households` actually reads `is_demo`.

    Pinning the property alone would have passed while the picker stayed broken: the first version
    of this fix had the branch and a `SELECT id, archetype` that could never reach it.
    """

    def test_list_households_carries_is_demo_through(self, db: Connection) -> None:
        with db.begin():
            db.execute(
                text(
                    "INSERT INTO households (id, archetype, is_demo) VALUES"
                    " ('hh_demo_biweekly', 'demo_biweekly', true),"
                    " ('hh_demo_plaid', NULL, true),"
                    " ('hh_real', NULL, false)"
                )
            )

        labels = {h.id: h.label for h in readpath.list_households(db)}
        assert labels == {
            "hh_demo_biweekly": "Biweekly, one card",
            "hh_demo_plaid": "Live Plaid data",
            "hh_real": "hh_real",
        }
