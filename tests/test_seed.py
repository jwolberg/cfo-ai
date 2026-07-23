"""The seeder, and the four households this engine had never seen.

The load-bearing test here is `TestTheRegressionOracle`. Everything else can be re-derived; that one
is the only thing standing between "the seeder walks the household `build()` walked" and "the seeder
walks *a* household and the numbers look plausible."

These need a real Postgres and say so loudly (`tests/conftest.py`). RLS, the composite keys, and the
`apr_source` column are the subject — a SQLite stand-in would test a different artifact and report
green, which is the failure `prd.md` §5.2 is about one level down.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import text

from backend import artifact as art
from backend.archetypes import ARCHETYPES
from backend.db.repository import Repository
from backend.seed import ENGINE_VERSION, household_id_for, seed, seed_all
from engine.models import Action, ReasonCode
from tests.conftest import requires_db

pytestmark = requires_db


def _rows(conn, sql: str, **p):
    return [dict(r) for r in conn.execute(text(sql), p).mappings()]


@pytest.fixture
def seeded(db_engine):
    """Every archetype, once. Session-expensive but the suite is small."""
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM decisions"))
        c.execute(text("TRUNCATE households CASCADE"))
    return {s.archetype: s for s in seed_all(db_engine)}


class TestTheRegressionOracle:
    """Archetype A's seeded decisions are the committed artifact's, or the seeder is walking a
    stranger.

    `sim.household.generate()` is not prefix-stable — `days` is part of the household's identity,
    not a window onto it — so a seeder that walked 151 days would produce a *different household*
    that still looked entirely reasonable. Every number would be plausible and none would be the
    demo's. This is the test that makes that a failure rather than a surprise in six weeks.
    """

    def test_archetype_a_is_demo_spec_itself(self):
        from backend.precompute import DEMO_SPEC

        assert ARCHETYPES["demo_biweekly"] is DEMO_SPEC

    def test_seeded_decisions_match_the_committed_artifact_day_for_day(self, db_engine, seeded):
        committed = art.load()
        hid = household_id_for("demo_biweekly")

        with db_engine.connect() as c:
            rows = _rows(
                c,
                "SELECT day, action, amount, target_card_id, projected_low_balance, reasons"
                " FROM decisions WHERE household_id = :h ORDER BY day",
                h=hid,
            )

        assert len(rows) == len(committed.days), "the served window is a different length"

        for row, rec in zip(rows, committed.days, strict=True):
            assert row["day"] == rec.day
            assert row["action"] == rec.decision.action.value, f"{rec.day}: action"
            assert Decimal(row["amount"]) == rec.decision.amount, f"{rec.day}: amount"
            assert row["target_card_id"] == rec.decision.target_debt_id, f"{rec.day}: target"

            codes = [r["code"] for r in row["reasons"]]
            assert codes == [r.code.value for r in rec.decision.reasons], f"{rec.day}: reasons"

    def test_the_projection_is_absent_where_the_artifact_says_it_is(self, db_engine, seeded):
        """A blocking refusal never ran a forecast. NULL is the honest record of that, and a zero
        would read as a perfect projection — `decision-engine.md` §9.1."""
        committed = art.load()
        hid = household_id_for("demo_biweekly")

        with db_engine.connect() as c:
            rows = _rows(
                c,
                "SELECT day, projected_low_balance FROM decisions"
                " WHERE household_id = :h ORDER BY day",
                h=hid,
            )

        for row, rec in zip(rows, committed.days, strict=True):
            expected = rec.decision.projected_low_balance
            if expected is None:
                assert row["projected_low_balance"] is None, f"{rec.day}: invented a projection"
            else:
                assert Decimal(row["projected_low_balance"]) == expected, f"{rec.day}"


class TestDeterminism:
    """Re-seeding converges. ADR-0002's staleness property, re-pointed at the database."""

    def test_reseeding_produces_identical_rows(self, db_engine, seeded):
        hid = household_id_for("demo_biweekly")

        def snapshot_of_rows():
            with db_engine.connect() as c:
                return _rows(
                    c,
                    "SELECT id, day, action, amount, target_card_id, projected_low_balance,"
                    " reasons, engine_version, snapshot_ref FROM decisions"
                    " WHERE household_id = :h ORDER BY day",
                    h=hid,
                )

        before = snapshot_of_rows()
        seed(db_engine, "demo_biweekly", ARCHETYPES["demo_biweekly"])
        after = snapshot_of_rows()

        assert before == after
        assert len(after) == 90

    def test_reseeding_does_not_accumulate(self, db_engine, seeded):
        hid = household_id_for("demo_biweekly")
        seed(db_engine, "demo_biweekly", ARCHETYPES["demo_biweekly"])

        with db_engine.connect() as c:
            for table in ("decisions", "snapshots"):
                n = c.execute(
                    text(f"SELECT count(*) FROM {table} WHERE household_id = :h"), {"h": hid}
                ).scalar()
                assert n == 90, f"{table} accumulated across re-seeds"

    def test_the_household_id_is_derived_not_random(self):
        assert household_id_for("demo_biweekly") == household_id_for("demo_biweekly")


class TestArchetypeB:
    """Three cards, mixed rates and behaviour. The first portfolio `_select_target` has ever ranked
    outside a unit test."""

    def test_the_target_is_the_highest_apr_targetable_card_not_the_highest_apr_card(
        self, db_engine, seeded
    ):
        """`card_b_transactor` holds the highest APR in the portfolio at 27.99% and must never be
        chosen: a transactor clears the statement every month, so the grace period already does what
        our sweep claims to do and moving their cash there is a prepayment we would be charging for
        (`prd.md` §7.2). A naive `max(apr)` picks it. The right answer is `card_b_high` at 24.99%.
        """
        hid = household_id_for("semimonthly_portfolio")

        with db_engine.connect() as c:
            targets = _rows(
                c,
                "SELECT DISTINCT target_card_id FROM decisions"
                " WHERE household_id = :h AND action = 'sweep'",
                h=hid,
            )

        chosen = {t["target_card_id"] for t in targets}
        assert chosen, "archetype B never swept — the ranking was never exercised"
        assert chosen == {"card_b_high"}
        assert "card_b_transactor" not in chosen

    def test_it_ranked_across_more_than_one_targetable_card(self, db_engine, seeded):
        """The assertion above is only worth anything if there was a choice to get wrong."""
        spec = ARCHETYPES["semimonthly_portfolio"]
        from engine.models import PaymentBehavior

        targetable = [c for c in spec.cards if c.behavior is not PaymentBehavior.TRANSACTOR]
        assert len(targetable) >= 2
        assert len({c.apr for c in targetable}) == len(targetable), "APRs must differ to rank"

    def test_each_card_carries_its_own_balance(self, db_engine, seeded):
        """Ticket 0027: one ledger for a whole portfolio reported a $3,000 card's balance as
        $14,009.20 — every card showing the collapsed portfolio *sum*. The proof the walk is no
        longer doing that is that the cards do **not** all report one identical balance.

        This used to assert three *distinct* balances, but the `0063` income fix let this
        semimonthly household sweep the days it was wrongly refused and **pay `card_b_high` to
        $0** — and a transactor always clears its statement to $0 too. So two cards legitimately
        share $0, for different reasons; "all distinct" now over-asserts. "Not collapsed to one
        value" is the property 0027 is actually about."""
        hid = household_id_for("semimonthly_portfolio")

        with db_engine.connect() as c:
            rows = _rows(
                c, "SELECT id, statement_balance FROM cards WHERE household_id = :h", h=hid
            )

        balances = {r["statement_balance"] for r in rows}
        assert len(rows) == 3
        assert len(balances) >= 2, f"cards collapsed to one balance: {rows}"
        portfolio_sum = sum(r["statement_balance"] for r in rows)
        assert not all(r["statement_balance"] == portfolio_sum for r in rows), (
            f"every card reports the portfolio sum — the 0027 collapse is back: {rows}"
        )


class TestArchetypeD:
    """The rate nobody will tell us.

    Note what is *not* asserted: `APR_UNKNOWN`. Ticket 0023's table asks for it and it cannot
    happen — `CardSpec.apr` is `Decimal`, `derive_card` reads
    `apr=card.apr if card.apr_reported else ESTIMATED_APR`, and the reason code fires only on
    `apr is None`. 0028 chose a visibility flag over a nullable rate deliberately: the card *has* a
    rate, Plaid merely does not report it. So the thing to assert is the absence of a claim.
    """

    def test_it_sweeps(self, db_engine, seeded):
        """An estimated rate is not a refusal. `APR_UNKNOWN` is not a safety gate — a wrong target
        optimizes worse and overdraws nobody, so the engine acts."""
        assert seeded["apr_unreported"].sweeps > 0

    def test_it_never_claims_a_saving(self, db_engine, seeded):
        """*Act on the estimate; never bill for it.* `interest.py` returns `None` for an ESTIMATED
        rate, so no sweep of this household may carry INTEREST_AVOIDED — otherwise `prd.md` §5.1's
        KPI is arithmetic on an invention."""
        hid = household_id_for("apr_unreported")

        with db_engine.connect() as c:
            rows = _rows(
                c,
                "SELECT day, action, reasons FROM decisions WHERE household_id = :h",
                h=hid,
            )

        swept = [r for r in rows if r["action"] == Action.SWEEP.value]
        assert swept, "D never swept, so the absence below proves nothing"

        for r in rows:
            codes = [x["code"] for x in r["reasons"]]
            assert ReasonCode.INTEREST_AVOIDED.value not in codes, (
                f"{r['day']}: claimed a saving against a guessed rate"
            )

    def test_the_estimate_is_stored_as_an_estimate(self, db_engine, seeded):
        """The defect this ticket found. `Repository.add_card` did not name `apr_source` in its
        INSERT, and the column carries `server_default 'reported'` — so a guessed 23% landed in the
        database as a rate an issuer had given us. A 23% estimate and a reported 23% are the same
        number; only this column tells them apart."""
        hid = household_id_for("apr_unreported")

        with db_engine.connect() as c:
            rows = _rows(c, "SELECT id, apr, apr_source FROM cards WHERE household_id = :h", h=hid)

        assert len(rows) == 2
        for r in rows:
            assert r["apr_source"] == "estimated", f"{r['id']}: a guess recorded as a fact"
            assert r["apr"] == Decimal("0.23000")

    def test_a_reported_rate_is_still_stored_as_reported(self, db_engine, seeded):
        """The other half — the fix must not simply write 'estimated' everywhere."""
        hid = household_id_for("demo_biweekly")

        with db_engine.connect() as c:
            rows = _rows(c, "SELECT apr_source FROM cards WHERE household_id = :h", h=hid)

        assert [r["apr_source"] for r in rows] == ["reported"]


class TestAddCardRefusesToGuessProvenance:
    """`apr_source` is required, and the omission must be an error rather than a default.

    This is the regression test for the defect above, and it is written against the repository
    rather than the seeder on purpose: the seeder happens to pass the column today, and the next
    caller will not have read this ticket.
    """

    def test_omitting_apr_source_raises(self, db):
        from datetime import date

        db.execute(text("INSERT INTO households (id, archetype) VALUES ('h_probe', 'probe')"))
        repo = Repository(conn=db, household_id="h_probe")

        with pytest.raises(Exception) as excinfo:
            repo.add_card(
                card_id="c1",
                apr=Decimal("0.23"),
                close_day_of_month=20,
                grace_days=21,
                statement_balance=Decimal("100"),
                statement_due_date=date(2026, 2, 10),
                minimum_payment=Decimal("25"),
                unbilled_balance=Decimal("0"),
                next_close_date=date(2026, 2, 20),
                behavior="revolver",
                observed_monthly_payment=None,
                observed_monthly_charges=None,
            )

        assert "apr_source" in str(excinfo.value)


class TestTheKeysAreScopedByHousehold:
    """Migration 0003. Two households may hold the same account id, because every household the walk
    derives holds `chk_demo` — and until this, the second one raised."""

    def test_two_households_share_account_ids(self, db_engine, seeded):
        with db_engine.connect() as c:
            rows = _rows(
                c,
                "SELECT id, count(*) AS n FROM accounts GROUP BY id HAVING count(*) > 1",
            )

        assert rows, "the archetypes no longer share an account id — this test proves nothing now"
        assert all(r["n"] == len(ARCHETYPES) for r in rows)


class TestEveryArchetypeIsSeeded:
    def test_all_four(self, seeded):
        assert set(seeded) == set(ARCHETYPES)

    def test_each_serves_the_whole_window(self, seeded):
        for name, s in seeded.items():
            assert s.days == 90, f"{name} served {s.days} days"
            assert s.sweeps + s.refusals == s.days

    def test_the_archetype_is_recorded_on_the_household(self, db_engine, seeded):
        """`households.archetype` is nullable so that synthetic and real are tellable apart in any
        number either one appears in."""
        with db_engine.connect() as c:
            rows = _rows(c, "SELECT id, archetype FROM households ORDER BY id")

        assert {r["archetype"] for r in rows} == set(ARCHETYPES)

    def test_every_decision_records_what_decided_it(self, db_engine, seeded):
        with db_engine.connect() as c:
            versions = _rows(c, "SELECT DISTINCT engine_version FROM decisions")

        assert [v["engine_version"] for v in versions] == [ENGINE_VERSION]

    def test_every_decision_points_at_a_snapshot_that_exists(self, db_engine, seeded):
        """`architecture.md` [3.3]: store the inputs, not references to the inputs. A dangling ref
        is the difference between an audit trail and a story."""
        with db_engine.connect() as c:
            dangling = _rows(
                c,
                "SELECT d.id FROM decisions d LEFT JOIN snapshots s"
                " ON s.id = replace(d.snapshot_ref, 'pg:', '')"
                " WHERE s.id IS NULL",
            )

        assert dangling == []


class TestTheIncomeGateIsCadenceAgnostic:
    """The finding, and its fix (ticket `0063`).

    Every archetype has `payroll.variation = 0.02` — their income is, by construction, exactly as
    regular as the demo household's. The income gate *used* to disagree: `income_variation` summed
    income into three 28-day buckets, which is two biweekly pay periods, so it read A as regular but
    aliased against every other cadence and refused B (semimonthly) and C (monthly) as
    `INCOME_TOO_VARIABLE` — measured, 37% and 21% of their days. That was pinned here as a tripwire
    "so that fixing it is a deliberate act with a measurement attached."

    This is that act. `income_variation` now measures the coefficient of variation of paycheck
    *amounts*, which is timing-agnostic, so a regular earner reads regular at any cadence. The
    measurement: the archetype breach rate fell on every shape (semimonthly 19.7%→11.0%, monthly
    7.6%→5.6%, biweekly/apr unchanged) with **zero** sweep-caused overdrafts — licensed by
    `calibrate`'s own rule. So the gate no longer fires on a household whose income is regular, and
    this test now pins *that*.
    """

    def test_every_archetype_has_identical_true_income_regularity(self):
        variations = {name: s.payroll.variation for name, s in ARCHETYPES.items()}
        assert len(set(variations.values())) == 1, variations

    def test_no_regular_earner_is_refused_as_too_variable(self, db_engine, seeded):
        """The fix: none of the four archetypes — biweekly, semimonthly, or monthly — is ever
        refused `income_too_variable`, because none of them has variable income. If this starts
        firing again, the metric regressed to aliasing against cadence."""
        refused = {}
        with db_engine.connect() as c:
            for name in ARCHETYPES:
                rows = _rows(
                    c,
                    "SELECT count(*) AS n FROM decisions WHERE household_id = :h"
                    " AND reasons @> :probe",
                    h=household_id_for(name),
                    probe=json.dumps([{"code": ReasonCode.INCOME_TOO_VARIABLE.value}]),
                )
                refused[name] = rows[0]["n"]

        assert refused == {name: 0 for name in ARCHETYPES}, (
            f"a regular earner was refused as too variable — the income metric is aliasing against "
            f"pay cadence again: {refused}"
        )
