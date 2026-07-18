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


def attested_for(repo: Repository) -> bool:
    """Whether this household's **current** cards are covered by a current attestation.

    True only when the latest attestation's fingerprint matches the fingerprint of the cards the
    household holds right now. A new card since the attestation → mismatch → False (drops the
    household back to `UNATTESTED`). No attestation at all → False.
    """
    current = card_fingerprint(c["id"] for c in repo.cards())
    attestation = repo.current_attestation()
    return attestation is not None and attestation["card_fingerprint"] == current
