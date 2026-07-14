---
id: "0011"
title: Simulator — actually charge the card
type: feature
status: todo
priority: high
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md
depends_on: ["0010"]
created: 2026-07-14
---

# Simulator — actually charge the card

Implements **U2** of the plan. Single owner: `backend-python-agent`.

**Goal:** Make `sim/household.py` issue card charges, drive the payment from behavior, and generate a
second card. Without this, nothing downstream is tested — no credit-card charge has ever been
simulated in this repo.

**Depends on:** `0010` (types).

**Files:** `sim/household.py`, `tests/test_household.py`, `tests/test_spend_model.py`,
`backend/precompute.py`, `tests/test_precompute.py`, `tests/test_outcome.py`

**Key points:**
- New `TxnKind.CARD_CHARGE` on a card ledger, distinct from checking transactions. Today every
  `TxnKind.DISCRETIONARY` txn hits *checking*.
- `SpendSpec` gains a `card_share`: that fraction of discretionary spend routes to the card ledger,
  the remainder to checking.
- `CardSpec.payment` stops being a constant and becomes a **function of `PaymentBehavior` and the
  closed statement balance** — precisely the real-world behavior this whole feature exists to model.
  Once the payment is determined by what was charged, there is no constant left to hardcode, and the
  workaround in `precompute.py` has nothing to stand on.
- `HouseholdSpec.card` → `HouseholdSpec.cards: tuple[CardSpec, ...]`, so the portfolio reserve and
  coverage gate have something to bite on.
- Determinism is non-negotiable: seed via the local `random.Random(seed)`, never module-level
  `random.*`.

## ⚠️ The rename is an import-time crash, not a failing test

**Read this before scoping the commit.** `HouseholdSpec(card=...)` is constructed at **module import
time** in `backend/precompute.py:136` (`DEMO_SPEC`), with seven further `spec.card` references in
that file, and again in `tests/test_precompute.py:36`, `tests/test_outcome.py:354`, and
`tests/test_spend_model.py:44`.

The moment `card` → `cards` lands, `import backend.precompute` raises `TypeError` and
`python -m backend.precompute` **cannot run at all**. Setting `card_share=0` does not rescue this —
the failure is in dataclass construction, not spend routing.

**So every construction site moves in the same commit.** That is why `backend/precompute.py`,
`tests/test_precompute.py`, and `tests/test_outcome.py` are in this ticket's file list rather than a
later one. `tests/test_outcome.py` matters especially: it is otherwise touched only by `0017`, which
deliberately does not gate shipping — so it would sit red indefinitely while the whole feature went
out.

**Expected, by design:** `tests/test_spend_model.py` will fail on its numbers. Its own docstring says
that is the point — *"They are expected to fail if and when the spend model is fixed."* **Re-derive
them; do not delete the tests.** `test_the_committed_artifact_is_the_one_the_code_generates` will
also fail; re-run `python -m backend.precompute` and **read the decision diff deliberately** — that
diff *is* the safety change.

**Test scenarios:** see the plan's U2 section — `card_share=0` reproduces today's history
byte-identically for the same seed; `card_share=1.0` routes every discretionary txn to the card; a
transactor's payment equals the previous statement's closed balance; a revolver's balance can *grow*;
two charges either side of the close land on different statements and leave checking a month apart;
the zero-inflated right-skewed spend shape survives the channel split.

**Verification:** `python -m backend.precompute` runs. `tests/test_spend_model.py` numbers re-derived
and passing, with a note explaining what moved and why.

Full detail: `docs/plans/2026-07-14-001-feat-card-spend-portfolio-reserve-plan.md` § U2.
