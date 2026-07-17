"""The platform funding account — the FBO the money transits (ticket 0041, U3).

Paying a card with the user's money is two legs joined by an account **we** hold: the debit leg
lands the pull in this account (an Increase account), and the payoff leg pays the creditor *from* it
(Method's `source` must be a platform account — an end-user's checking cannot be a source). That is
the FBO/custodial posture the rung commits to (ADR-0006, [5.2]).

The account is platform-owned, not household-scoped — one account for the whole tenant — so it lives
in config, not in a `HOUSEHOLD_SCOPED` table. In shadow it may be absent (nothing moves); a live
boot requires both identifiers, enforced beside the credential guard (see
`assert_transfer_funding_configured`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.db.session import transfer_mode


class TransferFundingNotConfigured(RuntimeError):
    """Raised at startup when live money movement is on but the funding account is unconfigured."""


@dataclass(frozen=True)
class FundingAccount:
    """The platform accounts each leg uses. `None` until provisioned (shadow needs neither).

    - `increase_account_id`: the Increase account the debit leg credits (funds land here).
    - `method_source_id`: the Method source account the payoff leg pays the creditor from.
    """

    increase_account_id: str | None
    method_source_id: str | None

    @property
    def is_configured(self) -> bool:
        return bool(self.increase_account_id) and bool(self.method_source_id)


def funding_account() -> FundingAccount:
    """The configured platform funding account, read from the environment.

    Identifiers, not secrets — an account id is not a credential (the credential is the API key /
    processor token, guarded in `backend/db/session.py`). Absent in shadow; required when live.
    """
    return FundingAccount(
        increase_account_id=os.environ.get("INCREASE_FUNDING_ACCOUNT_ID") or None,
        method_source_id=os.environ.get("METHOD_SOURCE_ACCOUNT_ID") or None,
    )


def assert_transfer_funding_configured() -> None:
    """Refuse a live boot with no funding account — there would be nowhere for the debit to land or
    the payoff to pay from. A no-op in shadow (the default), like the credential guard beside it."""
    if transfer_mode() != "live":
        return
    account = funding_account()
    if not account.is_configured:
        missing = [
            name
            for name, value in (
                ("INCREASE_FUNDING_ACCOUNT_ID", account.increase_account_id),
                ("METHOD_SOURCE_ACCOUNT_ID", account.method_source_id),
            )
            if not value
        ]
        raise TransferFundingNotConfigured(
            "TRANSFER_MODE is 'live' but the platform funding account is not configured: "
            f"missing {', '.join(missing)}. The debit leg has nowhere to land and the payoff leg "
            "nothing to pay from. Refusing to start."
        )
