"""The `TransferProvider` port and its shared value types (ticket 0040, U2).

One port, two legs. A `TransferProvider` fronts one vendor doing one kind of work — Increase for
the debit leg, Method for the payoff leg (U3/U4) — and the saga (U4) coordinates the two. The four
methods are deliberately shaped by what research found the real vendors do (KTD-3):

- **`authorize` is client-side.** Neither vendor has a true two-phase authorize/submit — Increase's
  `pending_approval` is an internal review hold you do not call, and Method's `dry_run` persists
  nothing. So `authorize` is *our* step: the pre-flight re-check (U4) plus deriving the idempotency
  key from the `(household, decision_date)` slot and the step (KTD-4). No vendor round-trip.
- **`submit`** is the one vendor create call — the point of no return. `ShadowProvider.submit` is a
  logged no-op (`architecture.md` [6]: move nothing first).
- **`status`** returns *our* `LedgerState`. Mapping each vendor's shape is the adapter's job, not
  the port's: Method reports one field (`pending→processing→sent→posted`); Increase has **no
  `settled` status** and settlement is derived (transfer submitted + a Transaction exists + no
  return in the window), so its adapter imposes `settled` as a timeout state. The port hides that.
- **`handle_return`** yields our `Return` or `None`. Increase surfaces a return as a separate event
  + a NACHA `return_code`; Method as further status on the same payment. The adapter reconciles;
  the port speaks one language.

This module imports nothing from `engine/` and touches no network — it is types and a Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

# The ledger's own vocabulary — the string sets the `transfers` CHECK constraints enforce (U1).
Leg = Literal["debit", "payoff"]
Direction = Literal["debit", "credit"]
Provider = Literal["increase", "method"]
LedgerState = Literal[
    "proposed",
    "authorized",
    "submitted",
    "pending",
    "settled",
    "returned",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class TransferIntent:
    """What a `Decision` authorizes for one leg — the input to `authorize()`.

    Frozen: an intent handed to a provider cannot be repointed at another household or amount, the
    same discipline the `Repository` holds (its `household_id` is immutable after construction).
    """

    household_id: str
    target_card_id: str
    decision_id: str
    decision_date: date
    leg: Leg
    direction: Direction
    amount: Decimal
    provider: Provider


@dataclass(frozen=True)
class Auth:
    """An authorized intent plus the idempotency key derived for its `submit` (KTD-4).

    The key is derived once, client-side, and reused on every retry of the same submission so a
    worker crash mid-retry cannot create two transfers against one slot — the guarantee Temporal
    would not give for free either (noted for the later saga substrate migration).
    """

    intent: TransferIntent
    idempotency_key: str


@dataclass(frozen=True)
class ProviderRef:
    """The vendor's own id for a submitted transfer — Increase's transfer id, Method's payment id.

    A shadow submission returns a synthetic ref (marked `shadow-`), because it called no vendor.
    """

    provider_transfer_id: str


@dataclass(frozen=True)
class Return:
    """A return or reversal outcome: the code the vendor gave, and the ledger state it lands in.

    `state` is `returned` or `failed` — a NACHA R-code (Increase) or a Method error code, normalized
    by the adapter into the single `return_code` the ledger records.
    """

    return_code: str
    state: LedgerState


@runtime_checkable
class TransferProvider(Protocol):
    """One vendor, one leg. The four operations the saga drives; each returns a typed result and
    does not itself write the ledger — the saga (U4) owns the append, so a real adapter stays pure
    vendor I/O. `@runtime_checkable` so a stub adapter can be asserted to satisfy the port."""

    def authorize(self, intent: TransferIntent) -> Auth:
        """Client-side: derive the idempotency key (and, for a live provider, re-run the freshness
        gates). No vendor call."""
        ...

    def submit(self, auth: Auth) -> ProviderRef:
        """Create the transfer at the vendor — the point of no return. Shadow: a logged no-op."""
        ...

    def status(self, ref: ProviderRef) -> LedgerState:
        """The transfer's current state in *our* vocabulary — the adapter maps the vendor shape."""
        ...

    def handle_return(self, ref: ProviderRef) -> Return | None:
        """A return/reversal if one has arrived, else `None`."""
        ...
