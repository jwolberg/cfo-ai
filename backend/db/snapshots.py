"""The `SnapshotStore` seam. Ticket 0022.

`architecture.md` [3.3] calls the frozen snapshot "the load-bearing design choice in the system"
and buys three things with it: **explanation** that stays true after Plaid rewrites history,
**audit** for a regulator, and **backtest** — replay any new engine version across every
historical snapshot and ask whether it would have overdrafted anyone.

> Store the inputs, not references to the inputs. The difference between an audit trail and a
> story.

## Why a seam, and the smaller claim it rests on

**Not volume.** An earlier draft of `architecture.md` [4.1] justified this with a *guessed* ~10KB
snapshot and 18 TB/yr, and reasoned a whole "phase 3" out of it. Measured over 90 consecutive
`Snapshot`s of the demo household:

| | mean/snapshot | vs raw |
|---|---|---|
| raw JSON | **2,699 B** | — |
| gzip'd individually | 770 B | 3.5× |
| gzip'd as a batch | 38 B | **70.7×** |

Consecutive days for one household are nearly identical — `today` moves, a few balances move, the
rest is unchanged. A store sorted by `(household_id, day)` exploits that; **Postgres TOAST cannot**,
because it compresses each value independently. At 5M households the gap is roughly a few hundred
dollars a month against a few tens.

**Real, worth two methods, and not an emergency.** So: the seam exists, Postgres JSONB backs it,
and object storage does not get built. [4.1]'s phase-3 trigger is measured and does not fire.

A reviewer will fairly ask whether this repeats [1.2]'s `FinancialProvider` mistake — *"you cannot
design the seam from n=1."* It does not: `put` and `get`, one known shape, one known consumer, not
an abstraction over vendors we have never called.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Protocol

from sqlalchemy import Connection, text

from backend.codec import CodecError, decode_tree, encode_tree
from engine.models import Snapshot


class SnapshotStoreError(CodecError):
    """A snapshot could not be stored or retrieved."""


class SnapshotStore(Protocol):
    """Three methods. That is the entire seam.

    `ref` is **opaque**: `"pg:<id>"` today, `"gs://bucket/key"` if the measurement ever changes.
    Nothing outside this module may parse, split, or pattern-match it — that is what makes moving
    the payload a one-file change rather than a migration.

    `get_many` is here because the read path renders a **window**, not a row (ticket `0024`): the
    feed is 90 days, and the display fields it needs — checking, savings, the buffer, the portfolio
    — live in the snapshot rather than on the `decisions` row. Ninety `get()` calls is not a
    correctness problem, it is a latency one, and the arithmetic is not close: the store is Neon,
    which is a network away, so ~90 x ~10ms of round trip is most of a second to draw one screen
    against ~10ms for a single query.

    **It is a batch, not a cache.** The alternative was denormalizing the display subset onto
    `decisions`, which duplicates what the snapshot already holds and invites the two to disagree —
    and `architecture.md` [4.1] is a whole section about not restructuring storage on an unmeasured
    guess.
    """

    def put(self, household_id: str, day: date, snapshot: Snapshot) -> str: ...

    def get(self, ref: str) -> Snapshot: ...

    def get_many(self, refs: Sequence[str]) -> dict[str, Snapshot]: ...


class PostgresSnapshotStore:
    """JSONB in the `snapshots` table. The backing this seam ships with.

    The connection is passed in rather than owned: writes must land in the **same transaction** as
    the `decisions` row that references them, or a crash between the two leaves a decision pointing
    at a snapshot that does not exist — an unexplainable sweep, which is the one thing
    `architecture.md` [3.3] exists to prevent.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def put(self, household_id: str, day: date, snapshot: Snapshot) -> str:
        payload = encode_tree(snapshot, error=SnapshotStoreError)
        snapshot_id = f"{household_id}:{day.isoformat()}"
        self._conn.execute(
            text(
                "INSERT INTO snapshots (id, household_id, day, payload)"
                " VALUES (:i, :h, :d, :p)"
                " ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload"
            ),
            # `json.dumps` rather than handing the dict to psycopg: the payload is already
            # tagged JSON-safe scalars, and letting the driver adapt it would invite exactly the
            # float conversion this whole module exists to prevent.
            {"i": snapshot_id, "h": household_id, "d": day, "p": json.dumps(payload)},
        )
        return f"pg:{snapshot_id}"

    def get(self, ref: str) -> Snapshot:
        payload = self._conn.execute(
            text("SELECT payload FROM snapshots WHERE id = :i"),
            {"i": self._id_of(ref)},
        ).scalar()
        if payload is None:
            raise SnapshotStoreError(f"no snapshot at {ref!r}")
        return decode_tree(Snapshot, payload, error=SnapshotStoreError)

    def get_many(self, refs: Sequence[str]) -> dict[str, Snapshot]:
        """Every snapshot in one query, keyed by the ref the caller asked with.

        Keyed by `ref` rather than by id so the caller never has to reverse the mapping — which
        would mean parsing the ref, which is the one thing refs exist to prevent.

        A missing ref is **not** silently dropped. The caller is rendering a decision that points
        here, and a feed that quietly skipped the day it could not explain would be
        `architecture.md` [3.3]'s "audit trail or a story" landing on the wrong side.
        """
        if not refs:
            return {}

        ids = {self._id_of(r): r for r in refs}
        rows = self._conn.execute(
            text("SELECT id, payload FROM snapshots WHERE id = ANY(:ids)"),
            {"ids": list(ids)},
        ).all()

        found = {
            ids[row.id]: decode_tree(Snapshot, row.payload, error=SnapshotStoreError)
            for row in rows
        }

        missing = set(refs) - set(found)
        if missing:
            raise SnapshotStoreError(f"no snapshot at {sorted(missing)}")

        return found

    @staticmethod
    def _id_of(ref: str) -> str:
        if not ref.startswith("pg:"):
            raise SnapshotStoreError(
                f"{ref!r} is not a Postgres snapshot ref. Refs are opaque to callers, but this "
                "store only understands its own — a 'gs://' ref means the payload moved and this "
                "store is the wrong one to ask."
            )
        return ref.removeprefix("pg:")
