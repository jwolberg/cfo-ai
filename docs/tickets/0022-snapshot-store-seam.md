---
id: "0022"
title: The SnapshotStore seam
type: feature
status: done
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0020"]
created: 2026-07-16
---

# The `SnapshotStore` seam

Implements **U4** of the plan. Single owner: `backend-python-agent`.

**Depends on:** `0020` (the `snapshots` table).

**Files:** `backend/db/snapshots.py`, `backend/artifact.py` (extract the codec),
`tests/test_snapshots.py`

## What to build

```python
class SnapshotStore(Protocol):
    def put(self, household_id: str, day: date, payload: bytes) -> str: ...
    def get(self, ref: str) -> bytes: ...
```

`PostgresSnapshotStore` writes the `snapshots` table and returns `"pg:<id>"`. `decisions.snapshot_ref`
holds that string and **nothing parses it** — that is the whole point of the seam.

## Why the seam exists — and the smaller claim it rests on

**Not volume.** An earlier draft of `architecture.md` [4.1] justified this with a *guessed* 10KB
snapshot and 18 TB/yr. Measured over 90 consecutive `Snapshot`s of the demo household:

| | mean/snapshot | vs raw |
|---|---|---|
| raw JSON | **2,699 B** | — |
| gzip'd individually | 770 B | 3.5× |
| gzip'd as a batch | 38 B | **70.7×** |

Consecutive days for one household are nearly identical. A store sorted by `(household_id, day)`
compresses ~70×; **Postgres TOAST compresses each value independently and gets ~3.5×.** At 5M
households that gap is roughly a few hundred dollars a month versus a few tens.

**Real, worth two methods, and not an emergency.** Build the seam; back it with Postgres JSONB;
do not build object storage. [`architecture.md`](../architecture.md) [4.1]'s phase-3 trigger is
measured and does not fire.

## The codec is promoted, not rewritten

`backend/artifact.py`'s `_encode`/`_decode` (lines 78-107) already does exactly this job: `Decimal` →
`{"$dec": "..."}`, `date` → `{"$date": ...}`, `Enum` → `{"$enum": [...]}`. **The snapshot payload is
JSON and still has no decimal type**, so the codec survives ADR-0002 for precisely this one use.
Extract it so both `artifact.py` and `snapshots.py` share one copy — **do not fork it.** This
ticket's whole context is a seam that drifted (`0019`); do not open a second one.

Note the asymmetry that keeps it honest: decoding a `$dec` uses `Decimal(str(...))` directly and is
**never re-quantized** through `money()` (see `artifact.py`'s module docstring, lines 20-30) — that
is deliberate, to avoid double-rounding. Preserve it.

## Acceptance criteria

- [ ] A `Snapshot` round-trips **exactly**: `get(put(s)) == s`, field by field, every `Decimal`
      identical. Include a value that would break as a float.
- [ ] **No float appears in either direction.** A test that asserts the encoded payload contains no
      JSON number where a money amount belongs.
- [ ] `ref` is opaque — a test asserts no caller parses, splits, or pattern-matches it. Grep-level
      is fine; the point is that swapping `"pg:<id>"` for `"gs://..."` touches one file.
- [ ] The codec exists **once**, shared by `artifact.py` and `snapshots.py`.
- [ ] `pytest` and `ruff` clean.

## Out of scope

Object storage, columnar encoding, compression. The seam makes all three a swap, and the measurement
says none is needed. Building them now is the speculation [`architecture.md`](../architecture.md)
[1.2] kills — *"you cannot design the seam from n=1."*
