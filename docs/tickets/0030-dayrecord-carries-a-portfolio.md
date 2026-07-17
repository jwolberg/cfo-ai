---
id: "0030"
title: DayRecord carries a portfolio — balance and APR per card, from what the engine saw
type: feature
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0027-a-ledger-per-card.md
depends_on: ["0023", "0027", "0028"]
created: 2026-07-16
---

# DayRecord carries a portfolio — balance and APR per card, from what the engine saw

**The decision `0027` deferred, now due.** `0027`'s own ticket said it needed "a decision about
`DayRecord`'s singular `debt_apr`/`debt_id`", and it punted: `walk()` learned to carry a portfolio
and `build()` learned to **raise** rather than report `cards[0]` of one.

**Blocks `0024`.** Three of the four archetypes `0023` seeded hold a portfolio, and the read path
cannot serve them through a record that holds one debt. Reporting `cards[0]` is the bug `0027`
exists to have fixed.

**Files:** `backend/artifact.py`, `backend/precompute.py`, `backend/main.py`,
`backend/data/decisions.json` (regenerated), `mobile/src/api/types.ts`,
`mobile/src/screens/Dashboard.tsx`, tests.

## The shape

`DayRecord.debt_balance` / `debt_apr` / `debt_id` become `debts: tuple[DebtRecord, ...]`:

```
DebtRecord: debt_id, balance, apr, apr_source
```

Balance **and APR per card** — a portfolio at two different rates is not one debt, and a household
holding 27.99% and 17.99% is owed the truth about which is which.

`Summary` keeps `targeted_debt_*` (the card we aim at — still well defined) and gains
`current_debt_balance`: the portfolio total on the last served day. `starting_debt_balance` becomes
the portfolio total on the first served day.

**Archetype A's numbers do not move**, because a one-card portfolio's total *is* that card. The hero
currently reads `starting_debt_balance → targeted_debt_balance`, which is coherent only while those
are the same card: on a portfolio it would compare a $14,700 total against a $9,000 card and render
$5,700 of progress that did not happen. Pairing `starting → current` is the same number for A and
the right one for B.

## ⚠️ The artifact records a rate the engine never saw

**Found while scoping this, and it is why the APR half is not optional.**

`build()` reads `debt_apr=spec.card.apr` (`backend/precompute.py:1103`) — and `spec` is `sim/`, the
ground truth the engine is **not allowed to see**. `summarize()` inherits it for
`targeted_debt_apr`. Measured, on a single-card household with `apr_reported=False`:

| | |
|---|---|
| what the engine saw in its snapshot | `apr=0.23, source=estimated` |
| what the spec knows | `apr=0.2399` |
| **what the artifact records** | **`0.2399`** |

The artifact's entire purpose is "what the engine decided, and the state it decided against"
(`DayRecord`'s own docstring). This is neither. It is the answer key, copied into the served record
for a card whose rate `0028` decided we would estimate and never claim.

It is invisible today for the reasons everything else in this repo is invisible until it isn't:
`DEMO_SPEC` reports its APR, so the two values coincide; `build()` raises on the multi-card
households where they would not; and neither `debt_apr` nor `targeted_debt_apr` currently crosses
the wire. **Putting APR per card on the wire is exactly what makes it visible** — the dashboard
would show 23.99% for a card we told the user we estimated at 23%, which is `0028` inverted.

**Every field in `debts[]` comes from `w.snapshot.portfolio.cards` — what the engine was shown —
and never from `spec`.** `apr_source` travels with the rate for the same reason it does in the
database (`0028`, and `0023`'s `add_card` defect): a 23% estimate and a reported 23% are the same
number, and only the provenance tells them apart.

## The artifact regenerates, and that needs a proof rather than a shrug

`SCHEMA_VERSION` 3 → 4, so `backend/data/decisions.json` changes bytes. `0019`'s rule stands and is
not waived: *"regenerating the committed artifact to match new output deletes the only evidence the
refactor preserved behavior."*

So the evidence moves rather than disappearing. **The decisions must be identical** — same action,
same amount, same target, same reasons, every day — and only the record's *shape* may change. Prove
it against the pre-change file in the diff, not by asserting it afterwards.

`build()` also stops raising on a multi-card spec, which is the point.

## Acceptance criteria

- [x] `DayRecord.debts` carries balance, APR and `apr_source` per card, sourced from the snapshot.
- [x] A test asserts `build()` never reads an APR from `spec` — a household with
      `apr_reported=False` records `0.23/estimated`, not its true rate.
      `TestTheArtifactRecordsWhatTheEngineSaw`.
- [x] `build()` serves a multi-card spec and no longer raises. `0027`'s guard test
      (`TestBuildStaysSingleCard`) is replaced by `TestBuildServesAPortfolio`, which checks what the
      guard stood in for: a portfolio arrives whole, each card with its own balance and rate.
- [x] **Archetype A's decisions are byte-identical to the pre-change artifact's.** Proven against a
      hash of the v3 file's decisions captured *before* the schema moved, not asserted afterwards:
      `sha256 d6d560c0…`, 90/90 days, every action, amount, target and reason identical.
- [x] `Summary.current_debt_balance` added; `starting_debt_balance` is the portfolio total. Every
      pre-existing summary field is unchanged — `current_debt_balance` equals
      `targeted_debt_balance` ($1,010.28) for the single-card demo, so A's hero renders identically.
- [x] `main.py` serves `debts[]`. `mobile/src/api/types.ts` follows, and the hero pairs
      `starting → current`.
- [x] `pytest` and `ruff` clean; mobile typecheck and 51 tests green.

## Out of scope

The read path itself (`0024`) and the switcher (`0025`). This ticket makes a portfolio
*representable*; those two make it reachable.
