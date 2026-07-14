"""Rendering a Decision into English.

The only place in the engine where copy lives. `decide()` produces `Reason` codes with
parameters; this turns them into sentences.

Why the separation is worth a file of its own:

- **Copy edits cannot break the decision.** Changing "paused" to "snoozed" is a change
  to this module, not to a financial calculation, and the test suite does not care.
- **The audit log stays stable.** A decision persisted in 2026 still means the same
  thing in 2029 after three rounds of UI copy. The code is the record; the sentence is
  a view of it.
- **The LLM has something to narrate *from*.** Handed a code and its parameters, a model
  can write a sentence for this particular household. Handed a finished sentence, all it
  can do is paraphrase — and any paraphrase of a financial claim is a chance to change
  its meaning.
- **Translation becomes possible at all**, rather than a rewrite of decide().

The tone is deliberate. A refusal is not an error and must never read like one: the user
is not being denied anything, they are being told their money is staying put and why.
"""

from __future__ import annotations

from decimal import Decimal

from engine.models import Decision, Reason, ReasonCode


def usd(amount: object) -> str:
    if isinstance(amount, Decimal):
        return f"${amount:,.2f}"
    return str(amount)


def days(n: object) -> str:
    """ "1 day", not "1 days". The engine hands us small integers and some of them are 1."""
    return f"{n} day" if n == 1 else f"{n} days"


def render(reason: Reason) -> str:
    p = reason.params

    match reason.code:
        case ReasonCode.FUNDING_ACCOUNT_MISSING:
            return "We can't see the account the payment would come from."

        case ReasonCode.FUNDING_ACCOUNT_NOT_CHECKING:
            return "Payments have to come from a checking account."

        case ReasonCode.CONNECTION_UNHEALTHY:
            return (
                "Your checking account needs reconnecting — until then we can't see your "
                "real balance, so we're not touching your money."
            )

        case ReasonCode.BALANCE_STALE:
            return (
                f"Your balance is {p['age_days']} days out of date. That's a guess, not a "
                "balance, and we don't move money against a guess."
            )

        case ReasonCode.INSUFFICIENT_HISTORY:
            return (
                f"We've only seen {p['have_days']} days of your account. We need about "
                f"{p['need_days']} to know when your paycheck and bills actually land — "
                "we're still learning, so we're leaving your cash alone."
            )

        case ReasonCode.INCOME_TOO_VARIABLE:
            return (
                "Your income moves around too much for us to promise a payment is safe. "
                "Holding that cash is the right call — the buffer is doing real work."
            )

        case ReasonCode.BLACKOUT:
            return "You've paused payments for today, so we haven't made one."

        case ReasonCode.SWEEP_IN_FLIGHT:
            return (
                f"Your last payment of {usd(p['amount'])} hasn't settled yet. We won't stack "
                "another on top of money the bank may not have taken out."
            )

        case ReasonCode.NO_DEBT:
            return "You have no debt left to pay. Nothing to do — congratulations."

        case ReasonCode.CARD_COVERAGE_INCOMPLETE:
            if p["unmatched"]:
                return (
                    "We can see a regular payment going to a card you haven't connected. Until "
                    "we know what's on it, we'd rather leave your cash where it is than pay "
                    "down one card and leave you short on another."
                )
            return (
                "Tell us this is all of your cards and we'll get started. We hold off until we "
                "can see the whole picture — paying the right card while another one goes short "
                "helps nobody."
            )

        case ReasonCode.CARD_BEHAVIOR_UNKNOWN:
            return (
                f"We've not yet seen enough statements on {p['card_count']} of your cards to "
                "know what you usually pay. A month or two more and we'll know what to set "
                "aside."
            )

        case ReasonCode.NO_INTEREST_TO_AVOID:
            return (
                "You clear your cards in full every month, so you're paying no interest for us "
                "to save. Your money is better off staying where it is."
            )

        case ReasonCode.APR_UNKNOWN:
            return (
                f"Your bank doesn't tell us the interest rates on your {p['card_count']} cards, "
                "so we can't tell which one is costing you the most. Add them and we'll start."
            )

        case ReasonCode.NO_SURPLUS:
            return (
                f"Your balance is heading for a low of {usd(p['low'])} on {p['low_day']}. "
                f"After your {usd(p['buffer'])} buffer and {usd(p['reserved'])} of minimum "
                "payments, there's nothing spare — so your cash stays where it is."
            )

        case ReasonCode.CADENCE_HOLD:
            return (
                f"We paid your card {days(p['days_since'])} ago, and we space payments at least "
                f"{days(p['min_days'])} apart. Your spare cash is safe where it is until then — "
                "we'd rather make one good payment than several small ones."
            )

        case ReasonCode.BELOW_MIN_SWEEP:
            return (
                f"What's left is under {usd(p['minimum'])}, which isn't worth moving. "
                "We'll look again tomorrow."
            )

        case ReasonCode.PROJECTION:
            return (
                f"Your balance is heading for a low of {usd(p['low'])} on {p['low_day']}, "
                f"after your {usd(p['buffer'])} buffer and {usd(p['reserved'])} set aside for "
                "your cards."
            )

        case ReasonCode.STATEMENT_RESERVED:
            return (
                f"We're holding back {usd(p['amount'])} of your cash for a statement due "
                f"{p['due']}."
            )

        case ReasonCode.PER_SWEEP_CAP:
            return f"Held to your {usd(p['cap'])} limit for a single payment."

        case ReasonCode.WEEKLY_CAP:
            return (
                f"Held to your {usd(p['cap'])} weekly limit — you've already put "
                f"{usd(p['already'])} toward the card this week."
            )

        case ReasonCode.CLEARS_THE_CARD:
            return "That clears the card."

        case ReasonCode.INTEREST_AVOIDED:
            return (
                f"That's about {usd(p['amount'])} of interest you won't pay, if you keep "
                "your payments where they are."
            )

        case ReasonCode.UNBILLED_ACCRUING:
            return (
                f"You've put {usd(p['amount'])} on this card since it last closed. That lands "
                f"on your next statement, due {p['due']}."
            )

        case ReasonCode.IDLE_CASH_ELSEWHERE:
            return (
                f"Separately: you're holding {usd(p['amount'])} in savings. It isn't cash we "
                "can move from here, and some of it should stay as your buffer — but a buffer "
                "that size belongs somewhere it earns interest, not in an account paying "
                "nothing while your card charges you."
            )

    raise ValueError(f"no copy for {reason.code}")  # pragma: no cover


def explain(decision: Decision) -> tuple[str, ...]:
    return tuple(render(r) for r in decision.reasons)
