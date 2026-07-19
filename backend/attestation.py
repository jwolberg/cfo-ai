"""Card-set attestation — the fingerprint, and whether a household's cards are currently attested.

Ticket 0050 (U5), KTD-7. The engine refuses to sweep a household whose card set it cannot be sure is
complete. A user *attests* — "these are all my cards" — and that clears the gate, but only while it
stays true: the attestation is scoped to a specific card set by a **fingerprint**, and the moment
a new card appears the fingerprint no longer matches and the household drops back to `UNATTESTED`.
That silent invalidation is the whole point (a stale attestation on an incomplete portfolio is the
exact failure the coverage gate exists to prevent).

`assemble_snapshot` stays a pure function of its inputs (it must, so `backend/replay.py` grades the
engine that shipped, not a second reconstruction — the same reason `sweeps_in_flight` is passed in),
so `attested` is *computed here* and passed in, rather than read inside it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from backend.db.repository import Repository

# A non-printable separator so two id lists cannot collide by concatenation (ab,c vs a,bc).
_SEP = "\x1f"


def card_fingerprint(card_ids: Iterable[str]) -> str:
    """A stable hash of a card set, order- and duplicate-independent.

    Sorted and de-duplicated first, so the fingerprint is a property of the *set* of cards, not the
    order they arrived in — attesting the same cards twice yields the same fingerprint, and adding
    or removing one changes it. An empty set has its own stable fingerprint.
    """
    joined = _SEP.join(sorted(set(card_ids)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def current_card_ids(repo: Repository) -> list[str]:
    """The ids of the cards this household holds **right now** — the set attestation fingerprints.

    A household has its cards in one of two places, never both, and this is the seam that lets one
    attestation mechanism cover both:

    - a **seeded** household's cards are engine `cards` rows (the demo, the seeder);
    - a **linked** household's cards are the Plaid **credit** accounts it ingested
      (`plaid_accounts`, migration 0014) — a linked household writes no engine `cards` row, so it
      once fingerprinted an *empty* set and could never be meaningfully attested.

    Engine cards win when present (a demo household is never linked); otherwise the Plaid credit
    accounts stand in, keyed by `plaid_account_id` — the same id `backend/linkedpath.py` gives the
    `CardSpec`, so the attested set and the decided set are the same cards. A new linked card
    changes this set, the fingerprint no longer matches, and coverage drops to `UNATTESTED` (KTD-7),
    exactly as it does for a seeded card.
    """
    engine_cards = [c["id"] for c in repo.cards()]
    if engine_cards:
        return engine_cards
    return [
        a["plaid_account_id"]
        for a in repo.latest_plaid_accounts()
        if (a.get("type") or "").lower() == "credit"
    ]


def attested_for(repo: Repository) -> bool:
    """Whether this household's **current** cards are covered by a current attestation.

    True only when the latest attestation's fingerprint matches the fingerprint of the cards the
    household holds right now (`current_card_ids` — engine cards or, for a linked household, its
    Plaid credit accounts). A new card since the attestation → mismatch → False (drops the household
    back to `UNATTESTED`). No attestation at all → False.
    """
    current = card_fingerprint(current_card_ids(repo))
    attestation = repo.current_attestation()
    return attestation is not None and attestation["card_fingerprint"] == current
