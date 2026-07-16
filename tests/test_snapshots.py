"""The `SnapshotStore` seam, and the codec under it. Ticket 0022.

The property that matters: a `Snapshot` goes in and the **same** `Snapshot` comes out. Not an
equal-looking one — `architecture.md` [3.3] wants the audit trail to answer "why did you move $220
that Tuesday" forever, and a cent that shifted in storage makes that answer a story.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.codec import CodecError, decode_tree, encode_tree
from backend.db.snapshots import PostgresSnapshotStore, SnapshotStoreError
from backend.precompute import (
    DEMO_POLICY,
    DEMO_SPEC,
    SEED,
    WINDOW_START,
    assemble_snapshot,
)
from engine.models import Snapshot
from sim.household import generate
from tests.conftest import requires_db


def _snapshot(day_offset: int = 90, quantile: float | None = None) -> Snapshot:
    history = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
    return assemble_snapshot(
        history=history,
        today=WINDOW_START + timedelta(days=day_offset),
        spec=DEMO_SPEC,
        policy=DEMO_POLICY,
        ledger_balances={DEMO_SPEC.card.card_id: Decimal("14000.00")},
        checking=Decimal("3200.00"),
        spend_quantile=quantile,
    )


class TestTheCodecRoundTripsWithoutADatabase:
    """The codec is the load-bearing half; it needs no Postgres to prove."""

    def test_a_snapshot_survives_exactly(self) -> None:
        s = _snapshot()
        assert decode_tree(Snapshot, encode_tree(s)) == s

    def test_it_survives_a_real_json_round_trip(self) -> None:
        """`==` on the encoded tree is not the test — JSONB goes through a serializer, and that is
        where a Decimal would become a float."""
        s = _snapshot()
        got = decode_tree(Snapshot, json.loads(json.dumps(encode_tree(s))))
        assert got == s

    def test_no_float_reaches_a_money_field(self) -> None:
        """The failure ADR-0002 [2.2] exists to prevent, asserted on the wire format itself: every
        money value is tagged text, so there is no JSON number for a float to hide in."""
        blob = json.dumps(encode_tree(_snapshot()))
        raw = json.loads(blob)

        assert raw["daily_discretionary_high"]["$dec"], "a money field is not tagged"
        for account in raw["accounts"]:
            assert isinstance(account["balance"], dict) and "$dec" in account["balance"]
        for card in raw["portfolio"]["cards"]:
            for field in ("statement_balance", "minimum_payment", "unbilled_balance"):
                assert "$dec" in card[field], f"card.{field} is not tagged"

    def test_an_apr_is_not_quantized_to_cents(self) -> None:
        """23.99% must not become 24%. An APR is a rate, and `money()`'s cent-quantization would
        destroy it — which is why decoding uses `Decimal(text)` and never `money(text)`."""
        s = _snapshot()
        card = decode_tree(Snapshot, encode_tree(s)).portfolio.cards[0]
        assert card.apr == Decimal("0.2399")
        assert str(card.apr) == "0.2399", "the exact digits, not a value that compares equal"

    def test_a_frozenset_field_survives_as_a_frozenset(self) -> None:
        """`UserPolicy.blackout_dates` is a frozenset and JSON has no such thing. It flattens to a
        list on the way out and must come back as a frozenset, or `today in blackout_dates` starts
        doing a linear scan over a type the engine never expected."""
        import dataclasses

        policy = dataclasses.replace(
            DEMO_POLICY, blackout_dates=frozenset({date(2026, 3, 5), date(2026, 3, 6)})
        )
        history = generate(DEMO_SPEC, WINDOW_START, 150, seed=SEED)
        s = assemble_snapshot(
            history=history,
            today=WINDOW_START + timedelta(days=90),
            spec=DEMO_SPEC,
            policy=policy,
            ledger_balances={DEMO_SPEC.card.card_id: Decimal("14000.00")},
            checking=Decimal("3200.00"),
        )
        got = decode_tree(Snapshot, json.loads(json.dumps(encode_tree(s))))
        assert isinstance(got.policy.blackout_dates, frozenset)
        assert got.policy.blackout_dates == policy.blackout_dates

    def test_the_optional_dial_survives_as_none_and_as_a_value(self) -> None:
        """`spend_30d_high` is `Decimal | None` and both cases are real — `None` is the shipped
        dial (`decision-engine.md` §6.6), and a value is what `calibrate.py` sweeps."""
        off = _snapshot(quantile=None)
        on = _snapshot(quantile=0.99)
        assert off.spend_30d_high is None
        assert on.spend_30d_high is not None

        assert decode_tree(Snapshot, encode_tree(off)).spend_30d_high is None
        assert decode_tree(Snapshot, encode_tree(on)).spend_30d_high == on.spend_30d_high

    def test_the_codec_cannot_drift_from_the_dataclass(self) -> None:
        """The reason this codec is generic rather than hand-written.

        A hand-written encoder needs a line per field and silently drops the next one someone adds:
        the payload still decodes, still validates, and quietly describes a different snapshot than
        the engine saw. That is ticket 0019's drift wearing a different hat. This asserts every
        field of `Snapshot` reaches the wire — so adding one cannot go unnoticed.
        """
        import dataclasses

        raw = encode_tree(_snapshot())
        assert set(raw) == {f.name for f in dataclasses.fields(Snapshot)}

    def test_an_untagged_decimal_is_rejected_rather_than_coerced(self) -> None:
        """A hand-edited payload with a bare float must fail loudly. Coercing it would be the
        silent rounding this whole module exists to prevent."""
        raw = encode_tree(_snapshot())
        raw["daily_discretionary_high"] = 86.55  # a float, as plain json.dumps would have written
        with pytest.raises(CodecError, match="expected a tagged decimal"):
            decode_tree(Snapshot, raw)

    def test_a_missing_field_is_rejected_rather_than_defaulted(self) -> None:
        raw = encode_tree(_snapshot())
        del raw["income_variation"]
        with pytest.raises(CodecError, match="missing field"):
            decode_tree(Snapshot, raw)


@requires_db
class TestThePostgresStore:
    def test_a_snapshot_round_trips_through_jsonb(self, db) -> None:
        s = _snapshot()
        with db.begin():
            db.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO households (id, archetype) VALUES ('h1','test')"
                )
            )
            store = PostgresSnapshotStore(db)
            ref = store.put("h1", s.today, s)
            got = store.get(ref)
        assert got == s

    def test_the_ref_is_opaque_to_the_caller(self, db) -> None:
        """Callers hold it and hand it back. Nothing parses it — that is what makes moving the
        payload to object storage a one-file change."""
        s = _snapshot()
        with db.begin():
            db.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO households (id, archetype) VALUES ('h1','test')"
                )
            )
            ref = PostgresSnapshotStore(db).put("h1", s.today, s)
        assert isinstance(ref, str)
        assert ref.startswith("pg:"), "today's backing; callers must not rely on this"

    def test_a_foreign_ref_is_refused_rather_than_guessed(self, db) -> None:
        """A `gs://` ref means the payload moved and this store is the wrong one to ask. Guessing
        would read the wrong snapshot, which is worse than an error."""
        with pytest.raises(SnapshotStoreError, match="not a Postgres snapshot ref"):
            PostgresSnapshotStore(db).get("gs://bucket/h1/2026-03-02")

    def test_a_missing_snapshot_raises_rather_than_returning_none(self, db) -> None:
        """A decision pointing at a snapshot that does not exist is an unexplainable sweep. It must
        be loud."""
        with pytest.raises(SnapshotStoreError, match="no snapshot"):
            PostgresSnapshotStore(db).get("pg:h1:2026-03-02")

    def test_writing_twice_is_idempotent(self, db) -> None:
        """The seeder is deterministic and re-runnable (ticket 0023). Re-seeding must not
        duplicate-key, and must not leave the first payload behind either."""
        s = _snapshot()
        with db.begin():
            db.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO households (id, archetype) VALUES ('h1','test')"
                )
            )
            store = PostgresSnapshotStore(db)
            first = store.put("h1", s.today, s)
            second = store.put("h1", s.today, s)
            assert first == second
            assert store.get(first) == s
