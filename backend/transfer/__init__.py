"""The write half of the sweep — money movement behind a provider port.

The sweep-execution rung (`docs/plans/2026-07-17-002-feat-sweep-execution-rung-plan.md`). Paying a
card is two legs: a **debit** (ACH pull from checking into a platform funding account) and a
**payoff** (a biller-payoff API landing that money on the card). Both sit behind the
`TransferProvider` port so either vendor swaps without the saga changing.

**Shadow mode is the first shipped state:** `ShadowProvider.submit()` is a logged no-op that moves
no money. The real vendor adapters (Increase, Method) and the durable saga land in later units; this
package deliberately imports nothing from `engine/` — the only coupling to the engine is the
`SWEEP_IN_FLIGHT` feedback, which the backend populates (U5), never a reverse import.
"""

from __future__ import annotations

from backend.transfer.provider import (
    Auth,
    Direction,
    LedgerState,
    Leg,
    Provider,
    ProviderRef,
    Return,
    TransferIntent,
    TransferProvider,
)
from backend.transfer.shadow import ShadowProvider, run_shadow_sweep

__all__ = [
    "Auth",
    "Direction",
    "Leg",
    "LedgerState",
    "Provider",
    "ProviderRef",
    "Return",
    "ShadowProvider",
    "TransferIntent",
    "TransferProvider",
    "run_shadow_sweep",
]
