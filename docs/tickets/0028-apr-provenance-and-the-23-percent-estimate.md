---
id: "0028"
title: APR provenance — estimate at 23%, but never claim a saving from a guess
type: feature
status: done
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md
depends_on: ["0027"]
created: 2026-07-16
---

# APR provenance — estimate at 23%, but never claim a saving from a guess

**Unblocks `0023`'s archetype D.** Settles `architecture.md` §7.5, one of the five decisions it
says must land before the first migration.

**Files:** `engine/models.py`, `engine/interest.py`, `sim/household.py`,
`backend/precompute.py`, `backend/db/models.py`, `alembic/versions/`, tests,
`docs/decision-engine.md`, `docs/architecture.md`

## The decision

`architecture.md` §7.5 offered three: user-entered, **estimated-with-lower-confidence**, or refuse
to rank (today). **We take the estimate, at 23%, and we carry the reduced confidence rather than
substituting silently.**

```python
class AprSource(str, Enum):
    REPORTED     = "reported"      # the issuer told us
    USER_ENTERED = "user_entered"  # the household told us
    ESTIMATED    = "estimated"     # we defaulted it — 23%
```

- **`decide.py` may rank on an estimate.** It already does, and needs no change: `_select_target`
  ranks any card whose `apr is not None`, so an estimated card at 0.23 sorts naturally and
  `APR_UNKNOWN` simply stops firing for it. The engine **acts** where it used to refuse.
- **`interest.py` must not claim a saving from an estimate.** `total_interest` returns `None`, so
  no `INTEREST_AVOIDED` reason is emitted and the dashboard says nothing about money saved.

## Why the second half is not optional

`decision-engine.md` §6.3 says the engine refuses to rank *"rather than guessing — a wrong target
card looks exactly like working while quietly destroying the entire value proposition."* That
warning is about **ranking**, and we are overriding it deliberately, with a reason (below).

The warning it does **not** cover is the claim. `prd.md` §5.1 makes *realized interest avoided* the
number the company is graded on, and §1 makes it the sentence the user reads — *"that's $31 of
interest you won't pay."* Computing either from a rate we invented would make the KPI partly
fiction. §5.1 already banned the *projected* KPI for being self-serving; a guessed APR is the same
disease in a different place.

So: **act on the estimate, never bill for it.**

## Why overriding §6.3's ranking warning is defensible

**`APR_UNKNOWN` is not a safety gate.** Every gate that protects the household — the buffer, the
forecast, the obligation reserve, the cadence — is untouched by this ticket. Paying down the wrong
card optimizes worse; it does not overdraft anyone. So this loosening cannot move `prd.md` §5.2's
guardrail, which is the one that outranks everything.

What it *can* cost is interest: 23% sits near the bottom of the persona's 20–30% band
(`prd.md` §3), so an estimated card will lose to most known cards in it. **That is the conservative
direction for a guess** — we under-prioritize the card we cannot see rather than diverting money
from one we can price.

## What archetype D becomes

Not an `APR_UNKNOWN` refusal any more. Something better: a household where the engine **sweeps
normally and tells you nothing about what it saved**, because it will not price a rate it guessed.
That is a real dashboard state, a real Plaid outcome (§6.3: "does not return APR for many
issuers"), and worth looking at before an issuer forces it.

`APR_UNKNOWN` stays live for `apr is None` — a card we decline to even estimate.

## Acceptance criteria

- [ ] `AprSource` on `engine/models.py`; `Card.apr_source`, defaulting to `REPORTED` so nothing
      existing moves.
- [ ] `CardSpec.apr_reported: bool = True` in `sim/`. **Not a nullable `CardSpec.apr`** — the card
      *has* a rate, Plaid merely does not report it. `sim/` models the world; the derivation models
      what we can see. A card with no rate is also un-accruable by `DebtLedger`.
- [ ] `derive_card` sets `apr=ESTIMATED_APR, apr_source=ESTIMATED` when the spec says unreported,
      and passes the real rate through otherwise.
- [ ] **`interest.py` returns `None` for an ESTIMATED card**, so no `INTEREST_AVOIDED` reason is
      emitted. Asserted directly.
- [ ] **A sweep still happens** on an all-estimated portfolio — the engine acts, and the reason
      list carries no interest claim.
- [ ] `cards.apr_source` column + migration. `0020`'s `apr` is already nullable.
- [ ] **`backend/data/decisions.json` byte-identical, not regenerated.** The demo card's APR is
      reported, so nothing may move.
- [ ] **`calibrate.measure(None)` unchanged**: 4,320 / 2.338% / 0 / $544,640.58. The population is
      all-reported.
- [ ] `decision-engine.md` §6.3 and `architecture.md` §7.5 updated — §7.5 stops being an open
      decision.

## Out of scope

`USER_ENTERED` has no entry path yet; the enum carries it because the schema needs to know the
provenance exists (§7.5) and adding the column later is the expensive half. §6.3's *"a real product
needs a user-entered fallback"* is still true and still unbuilt.
