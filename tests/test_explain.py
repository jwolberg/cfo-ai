"""The copy layer.

These tests exist to protect two properties, and neither of them is "the wording is
nice":

1. Every ReasonCode can be rendered. A decision that cannot be explained is not
   shippable, and a missing template must fail in CI rather than in front of a customer
   who has just had money moved.
2. Copy is not load-bearing. Rewording anything here changes no decision, which is the
   whole reason the codes and the prose were separated.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.explain import explain, render
from engine.models import Action, Decision, Reason, ReasonCode, money

# One representative set of params per code. Update this when a code is added — the
# test below will fail until you do, which is the point.
SAMPLES: dict[ReasonCode, dict] = {
    ReasonCode.CARD_COVERAGE_INCOMPLETE: {"coverage": "unmatched_payment", "unmatched": 1},
    ReasonCode.CARD_BEHAVIOR_UNKNOWN: {"card_count": 1},
    ReasonCode.NO_INTEREST_TO_AVOID: {"card_count": 2},
    ReasonCode.STATEMENT_RESERVED: {"amount": money("2240.00"), "due": date(2026, 3, 13)},
    ReasonCode.UNBILLED_ACCRUING: {"amount": money("1240.00"), "due": date(2026, 4, 10)},
    ReasonCode.FUNDING_ACCOUNT_MISSING: {"account_id": "chk"},
    ReasonCode.FUNDING_ACCOUNT_NOT_CHECKING: {"kind": "savings"},
    ReasonCode.CONNECTION_UNHEALTHY: {"state": "login_required"},
    ReasonCode.BALANCE_STALE: {"age_days": 4},
    ReasonCode.INSUFFICIENT_HISTORY: {"have_days": 12, "need_days": 60},
    ReasonCode.INCOME_TOO_VARIABLE: {"variation": 0.61, "limit": 0.25},
    ReasonCode.BLACKOUT: {"date": date(2026, 7, 12)},
    ReasonCode.SWEEP_IN_FLIGHT: {"amount": money("200.00")},
    ReasonCode.NO_DEBT: {},
    ReasonCode.APR_UNKNOWN: {"card_count": 2},
    ReasonCode.NO_SURPLUS: {
        "low": money("800.00"),
        "low_day": date(2026, 7, 20),
        "buffer": money("750.00"),
        "reserved": money("180.00"),
    },
    ReasonCode.BELOW_MIN_SWEEP: {"minimum": money("1.00")},
    ReasonCode.CADENCE_HOLD: {
        "days_since": 3,
        "min_days": 7,
        "days_until": 4,
        "amount": money("500.00"),
    },
    ReasonCode.PROJECTION: {
        "low": money("3880.00"),
        "low_day": date(2026, 7, 15),
        "buffer": money("750.00"),
        "reserved": money("180.00"),
    },
    ReasonCode.INTEREST_AVOIDED: {"amount": money("31.00"), "debt_id": "visa"},
    ReasonCode.PER_SWEEP_CAP: {"cap": money("300.00")},
    ReasonCode.WEEKLY_CAP: {"cap": money("600.00"), "already": money("450.00")},
    ReasonCode.CLEARS_THE_CARD: {"debt_id": "visa"},
    ReasonCode.IDLE_CASH_ELSEWHERE: {"amount": money("20000.00")},
}


def test_every_reason_code_has_copy():
    """A new code with no sentence must fail here, not in production."""
    missing = set(ReasonCode) - set(SAMPLES)
    assert not missing, f"no sample params for {missing}"

    for code, params in SAMPLES.items():
        text = render(Reason(code, params))
        assert text and text[0].isupper() and text.rstrip().endswith((".", "!"))


@pytest.mark.parametrize("code", list(ReasonCode))
def test_no_raw_placeholder_leaks_into_the_copy(code):
    text = render(Reason(code, SAMPLES[code]))

    assert "{" not in text and "}" not in text
    assert "Decimal" not in text, "a raw Decimal repr reached the customer"
    assert "None" not in text


def test_money_is_formatted_for_humans():
    text = render(Reason(ReasonCode.IDLE_CASH_ELSEWHERE, {"amount": money("20000.00")}))

    assert "$20,000.00" in text


def test_a_refusal_does_not_read_like_an_error():
    """The user is not being denied something. They are being told their money is
    staying put, and why. Tone is a product requirement here, not decoration.
    """
    for code in (
        ReasonCode.INCOME_TOO_VARIABLE,
        ReasonCode.NO_SURPLUS,
        ReasonCode.BALANCE_STALE,
        ReasonCode.INSUFFICIENT_HISTORY,
    ):
        text = render(Reason(code, SAMPLES[code])).lower()

        for word in ("error", "failed", "invalid", "denied", "sorry", "unable"):
            assert word not in text, f"{code} reads like a failure: {text!r}"


def test_explain_renders_a_whole_decision_in_order():
    d = Decision(
        action=Action.SWEEP,
        amount=Decimal("300.00"),
        target_debt_id="visa",
        reasons=(
            Reason(ReasonCode.PROJECTION, SAMPLES[ReasonCode.PROJECTION]),
            Reason(ReasonCode.PER_SWEEP_CAP, SAMPLES[ReasonCode.PER_SWEEP_CAP]),
        ),
    )

    lines = explain(d)

    assert len(lines) == 2
    assert "$3,880.00" in lines[0]
    assert "$300.00" in lines[1]


def test_a_single_day_is_not_pluralised():
    """ "We paid your card 1 days ago" shipped in the first draft of the cadence copy.

    Both numbers in this sentence are days, and both can be 1.
    """
    text = render(
        Reason(
            ReasonCode.CADENCE_HOLD,
            {"days_since": 1, "min_days": 7, "days_until": 1, "amount": money("500.00")},
        )
    )

    assert "1 day ago" in text
    assert "in 1 day." in text
    assert "1 days" not in text


def test_a_cadence_hold_does_not_read_like_an_error():
    """A refusal tells the user their money is staying put. It does not scold them."""
    text = render(
        Reason(
            ReasonCode.CADENCE_HOLD,
            {"days_since": 3, "min_days": 7, "days_until": 4, "amount": money("500.00")},
        )
    )

    assert not any(w in text.lower() for w in ("error", "cannot", "failed", "denied", "invalid"))


def test_the_cadence_hold_says_what_we_paid_and_when_we_are_back():
    """The copy the user actually asked for: what they got, and when we return.

    It no longer explains our *cadence policy* ("we space payments at least 7 days apart") —
    a rule the user did not ask for and cannot act on. It tells them the two things they can:
    we paid this much, and we look again then.
    """
    text = render(
        Reason(
            ReasonCode.CADENCE_HOLD,
            {"days_since": 6, "min_days": 7, "days_until": 1, "amount": money("500.00")},
        )
    )

    assert text == "We paid an extra $500.00 for you 6 days ago. Our next check-in is in 1 day."


def test_a_cadence_hold_never_invents_an_amount_it_does_not_have():
    """`last_sweep_amount=None` means we do not know the figure — so the sentence omits it.

    Same rule as NO_INTEREST_TO_AVOID: the engine never renders a number it cannot stand
    behind, and the absence is structural rather than a formatting accident. A `$None` or a
    `$0.00` here would be a lie about a payment we actually made.
    """
    text = render(
        Reason(
            ReasonCode.CADENCE_HOLD,
            {"days_since": 6, "min_days": 7, "days_until": 1, "amount": None},
        )
    )

    assert text == "We paid your card 6 days ago. Our next check-in is in 1 day."
    assert "None" not in text
    assert "$" not in text
