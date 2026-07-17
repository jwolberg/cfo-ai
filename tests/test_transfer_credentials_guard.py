"""The money-movement startup guards (ticket 0041, U3).

Same shape and spirit as `TestPlaidTokenStartGuard`: the failure is silent (a plaintext credential,
an unconfigured FBO), so the guard is a startup refusal keyed on the switch that turns real money on
— `TRANSFER_MODE`. No database needed; these are environment gates.
"""

from __future__ import annotations

import pytest

from backend.db.session import (
    TransferCredentialWouldLeak,
    assert_transfer_credentials_safe_at_rest,
    transfer_mode,
)
from backend.transfer.funding import (
    TransferFundingNotConfigured,
    assert_transfer_funding_configured,
)


def test_shadow_is_the_default_and_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSFER_MODE", raising=False)
    assert transfer_mode() == "shadow"
    assert_transfer_credentials_safe_at_rest()  # does not raise
    assert_transfer_funding_configured()  # does not raise


def test_shadow_boots_even_with_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow moves no money, so nothing needs protecting — the guards are no-ops."""
    monkeypatch.setenv("TRANSFER_MODE", "shadow")
    monkeypatch.delenv("METHOD_API_KEY", raising=False)
    assert_transfer_credentials_safe_at_rest()  # does not raise


def test_live_is_refused_while_credentials_are_unsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both credential paths are honestly unbuilt (KMS, secrets manager), so a live boot cannot
    succeed today — the point of KTD-7: real money does not turn on before the credential story is
    real."""
    monkeypatch.setenv("TRANSFER_MODE", "live")
    with pytest.raises(TransferCredentialWouldLeak) as e:
        assert_transfer_credentials_safe_at_rest()
    msg = str(e.value)
    assert "debit credential" in msg
    assert "secrets manager" in msg
    assert "submit()" in msg  # names the consequence, so the fix is not "grant more"


def test_live_is_refused_when_the_funding_account_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSFER_MODE", "live")
    monkeypatch.delenv("INCREASE_FUNDING_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("METHOD_SOURCE_ACCOUNT_ID", raising=False)
    with pytest.raises(TransferFundingNotConfigured, match="INCREASE_FUNDING_ACCOUNT_ID"):
        assert_transfer_funding_configured()


def test_the_funding_guard_is_a_noop_in_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSFER_MODE", "shadow")
    monkeypatch.delenv("INCREASE_FUNDING_ACCOUNT_ID", raising=False)
    assert_transfer_funding_configured()  # does not raise despite no config
