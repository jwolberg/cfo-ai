"""The generated decision-history artifact: schema, (de)serialization, validation.

The demo's persistence layer is one JSON file — see
`docs/decisions/0002-generated-json-artifact-over-database.md`. `backend/precompute.py`
writes it; `backend/main.py` loads and validates it once at startup and serves it from
memory. There is no database, and there is no write path at runtime.

## Why the encoding is tagged

`Decision.reasons` carries `Reason.params`, an open `Mapping[str, object]` whose values are
`Decimal` dollar amounts, `date`s, enums, ints, floats, and strings depending on the code.
Plain `json.dumps` would flatten every one of those to a string or a float, and a `Decimal`
that round-trips through a float is no longer the cent the engine decided on. So each
scalar is tagged with its type on the way out and reconstructed on the way in, and a
round-trip is exact — `tests/test_precompute.py` asserts it.

Floats never touch a dollar amount, in either direction. `money()` refuses them
(`engine/models.py`), and this module never hands one to it.

## Decoding a Decimal does not re-quantize

Amounts are written as the exact decimal text `money()` already produced, and read back
with `Decimal(text)` rather than `money(text)`. Re-quantizing on the way in would be a
second rounding of an already-rounded figure — silent, and in the one place the whole type
system exists to keep silent roundings out of. `validate()` checks the amounts are
cent-quantized instead, so a hand-edited artifact still fails loudly rather than being
quietly fixed up.

An APR is the reason this matters in practice: it is a *rate*, not money, and quantizing
0.2399 to cents would turn 23.99% into 24%.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from engine.models import (
    CENTS,
    ZERO,
    AccountKind,
    Action,
    ConnectionState,
    Decision,
    EventKind,
    Reason,
    ReasonCode,
)

SCHEMA_VERSION = 3

# Where the committed artifact lives. Shipped in the source tree (not built at deploy
# time) so Cloud Run's buildpacks package it with everything else — see the plan's
# Deployment decisions.
DEFAULT_PATH = Path(__file__).parent / "data" / "decisions.json"

_ENUMS: dict[str, type[Enum]] = {
    cls.__name__: cls for cls in (Action, ReasonCode, AccountKind, ConnectionState, EventKind)
}


class ArtifactError(ValueError):
    """The artifact is missing, malformed, or internally inconsistent.

    Raised at startup, never per-request: a service holding bad data should refuse to
    come up rather than serve 500s (or, worse, wrong numbers) one request at a time.
    """


# --- scalar encoding ----------------------------------------------------------------


def _encode(value: object) -> Any:
    # bool before int (a bool *is* an int), Enum before str (our enums are str enums).
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Enum):
        return {"$enum": [type(value).__name__, value.value]}
    if isinstance(value, int | float | str):
        return value
    raise ArtifactError(f"cannot encode {value!r} of type {type(value).__name__}")


def _decode(value: Any) -> object:
    if not isinstance(value, dict):
        return value

    if "$dec" in value:
        return Decimal(str(value["$dec"]))  # exact text, never re-quantized — see module docs
    if "$date" in value:
        return date.fromisoformat(str(value["$date"]))
    if "$enum" in value:
        name, raw = value["$enum"]
        if name not in _ENUMS:
            raise ArtifactError(f"unknown enum type {name!r}")
        return _ENUMS[name](raw)

    raise ArtifactError(f"unrecognised tagged value {value!r}")


def _decimal(raw: Any, field: str) -> Decimal:
    decoded = _decode(raw)
    if not isinstance(decoded, Decimal):
        raise ArtifactError(f"{field} is not a decimal: {raw!r}")
    return decoded


# --- records ------------------------------------------------------------------------


@dataclass(frozen=True)
class DayRecord:
    """One served day: what the engine decided, and the state it decided against.

    The snapshot fields are the display subset the dashboard needs (R3) — not the whole
    `Snapshot`. The engine's full input isn't reconstructible from this and isn't meant
    to be; `decide()` already ran, at build time, and its output is what's being served.
    """

    day: date
    decision: Decision
    checking_balance: Decimal
    savings_balance: Decimal
    buffer_floor: Decimal
    debt_balance: Decimal
    debt_apr: Decimal | None
    debt_id: str
    history_days: int

    @property
    def paid_off(self) -> bool:
        """ "Paid off" is a REFUSE carrying NO_DEBT — never a third Action.

        `Action` has exactly two values (`engine/models.py`). Every layer that shows a
        "paid off" state derives it the same way, from here.
        """
        return self.decision.action is Action.REFUSE and self.decision.has(ReasonCode.NO_DEBT)

    def to_dict(self) -> dict[str, Any]:
        d = self.decision
        return {
            "day": self.day.isoformat(),
            "decision": {
                "action": d.action.value,
                "amount": _encode(d.amount),
                "target_debt_id": d.target_debt_id,
                "projected_low_balance": _encode(d.projected_low_balance),
                "reasons": [
                    {
                        "code": r.code.value,
                        "params": {k: _encode(v) for k, v in r.params.items()},
                    }
                    for r in d.reasons
                ],
            },
            "checking_balance": _encode(self.checking_balance),
            "savings_balance": _encode(self.savings_balance),
            "buffer_floor": _encode(self.buffer_floor),
            "debt_balance": _encode(self.debt_balance),
            "debt_apr": _encode(self.debt_apr),
            "debt_id": self.debt_id,
            "history_days": self.history_days,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DayRecord:
        if not isinstance(raw, dict):
            raise ArtifactError(f"day record is not an object: {raw!r}")

        try:
            d = raw["decision"]
            low = d["projected_low_balance"]
            apr = raw["debt_apr"]
            decision = Decision(
                action=Action(d["action"]),
                amount=_decimal(d["amount"], "decision.amount"),
                target_debt_id=d["target_debt_id"],
                reasons=tuple(
                    Reason(
                        code=ReasonCode(r["code"]),
                        params={k: _decode(v) for k, v in r["params"].items()},
                    )
                    for r in d["reasons"]
                ),
                projected_low_balance=(
                    None if low is None else _decimal(low, "decision.projected_low_balance")
                ),
            )
            return cls(
                day=date.fromisoformat(raw["day"]),
                decision=decision,
                checking_balance=_decimal(raw["checking_balance"], "checking_balance"),
                savings_balance=_decimal(raw["savings_balance"], "savings_balance"),
                buffer_floor=_decimal(raw["buffer_floor"], "buffer_floor"),
                debt_balance=_decimal(raw["debt_balance"], "debt_balance"),
                debt_apr=None if apr is None else _decimal(apr, "debt_apr"),
                debt_id=raw["debt_id"],
                history_days=int(raw["history_days"]),
            )
        except ArtifactError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError(f"malformed day record: {exc}") from exc


@dataclass(frozen=True)
class Summary:
    """The dashboard's headline stats (R3), computed once from the served window."""

    interest_avoided_total: Decimal
    total_swept: Decimal
    current_buffer: Decimal
    targeted_debt_id: str | None
    targeted_debt_balance: Decimal
    targeted_debt_apr: Decimal | None
    # What the card owed on the **first** served day. The denominator of "how far down is it",
    # and the only honest one available: the UI cannot derive it, because the per-day
    # `debt_balance` does not cross the wire.
    #
    # Note this measures the *card's* progress, not ours. The household's own payments are in
    # it alongside our sweeps, and the copy must not claim otherwise.
    starting_debt_balance: Decimal
    sweep_count: int
    refuse_count: int
    paid_off: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "interest_avoided_total": _encode(self.interest_avoided_total),
            "total_swept": _encode(self.total_swept),
            "current_buffer": _encode(self.current_buffer),
            "targeted_debt_id": self.targeted_debt_id,
            "targeted_debt_balance": _encode(self.targeted_debt_balance),
            "targeted_debt_apr": _encode(self.targeted_debt_apr),
            "starting_debt_balance": _encode(self.starting_debt_balance),
            "sweep_count": self.sweep_count,
            "refuse_count": self.refuse_count,
            "paid_off": self.paid_off,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Summary:
        if not isinstance(raw, dict):
            raise ArtifactError(f"summary is not an object: {raw!r}")
        try:
            apr = raw["targeted_debt_apr"]
            return cls(
                interest_avoided_total=_decimal(
                    raw["interest_avoided_total"], "interest_avoided_total"
                ),
                total_swept=_decimal(raw["total_swept"], "total_swept"),
                current_buffer=_decimal(raw["current_buffer"], "current_buffer"),
                targeted_debt_id=raw["targeted_debt_id"],
                targeted_debt_balance=_decimal(
                    raw["targeted_debt_balance"], "targeted_debt_balance"
                ),
                targeted_debt_apr=None if apr is None else _decimal(apr, "targeted_debt_apr"),
                starting_debt_balance=_decimal(
                    raw["starting_debt_balance"], "starting_debt_balance"
                ),
                sweep_count=int(raw["sweep_count"]),
                refuse_count=int(raw["refuse_count"]),
                paid_off=bool(raw["paid_off"]),
            )
        except ArtifactError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError(f"malformed summary: {exc}") from exc


def summarize(days: tuple[DayRecord, ...]) -> Summary:
    """Roll the served window up into the dashboard's headline stats.

    Interest avoided is summed from the engine's own `INTEREST_AVOIDED` reasons, not
    recomputed here. The engine emits that reason only when it can stand behind the claim
    (`engine/interest.py`), so summing the emitted ones inherits that discipline for free —
    a re-derivation here would be a second implementation of the number the company is
    graded on, and the day the two disagreed we would not know which one was in the UI.
    """
    if not days:
        raise ArtifactError("cannot summarize an empty served window")

    interest = ZERO
    swept = ZERO
    sweeps = 0
    refusals = 0

    for record in days:
        if record.decision.action is Action.SWEEP:
            sweeps += 1
            swept += record.decision.amount
        else:
            refusals += 1

        for reason in record.decision.reasons:
            if reason.code is ReasonCode.INTEREST_AVOIDED:
                amount = reason.params["amount"]
                assert isinstance(amount, Decimal)  # decide() puts a money() Decimal here
                interest += amount

    last = days[-1]
    return Summary(
        interest_avoided_total=interest,
        total_swept=swept,
        current_buffer=last.buffer_floor,
        targeted_debt_id=None if last.paid_off else last.debt_id,
        targeted_debt_balance=last.debt_balance,
        targeted_debt_apr=last.debt_apr,
        starting_debt_balance=days[0].debt_balance,
        sweep_count=sweeps,
        refuse_count=refusals,
        paid_off=last.paid_off,
    )


@dataclass(frozen=True)
class SpendSnapshot:
    """What the household spends, and what their card is about to take.

    Comprehension, not a decision. Every figure here is *reported*; none of it feeds
    `forecast.py`. Swapping the forecast onto `worst_30d` would **loosen** the reserve, and
    loosening needs the measured breach rate `engine/outcome.py` cannot yet produce (U8).

    The two obligations are kept separate because they are due a **month apart**, and a single
    "what you owe" number hides exactly the thing the user needs to see.
    """

    # The statement that has already closed. Inside the horizon, and reserved.
    statement_balance: Decimal
    statement_due: date
    # Charged since that close. Not yet due — this is next month's bill, forming now.
    unbilled_balance: Decimal
    unbilled_due: date
    # What the engine is holding back for the closed statement. The line that ties the
    # dashboard to the reserve and stops it looking arbitrary.
    reserved: Decimal

    # Every overlapping 30-day total in the trailing window, by channel. The strip chart.
    rolling_30d_cash: tuple[Decimal, ...]
    rolling_30d_card: tuple[Decimal, ...]

    # Did the card grow last cycle? If charges outran payments the sweep is not their answer,
    # and the product should say so rather than staying quiet about it.
    charged_last_cycle: Decimal
    paid_last_cycle: Decimal

    @property
    def card_grew_by(self) -> Decimal:
        return self.charged_last_cycle - self.paid_last_cycle

    @property
    def worst_30d_cash(self) -> Decimal:
        return max(self.rolling_30d_cash, default=ZERO)

    @property
    def worst_30d_card(self) -> Decimal:
        return max(self.rolling_30d_card, default=ZERO)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_balance": _encode(self.statement_balance),
            "statement_due": self.statement_due.isoformat(),
            "unbilled_balance": _encode(self.unbilled_balance),
            "unbilled_due": self.unbilled_due.isoformat(),
            "reserved": _encode(self.reserved),
            "rolling_30d_cash": [_encode(v) for v in self.rolling_30d_cash],
            "rolling_30d_card": [_encode(v) for v in self.rolling_30d_card],
            "charged_last_cycle": _encode(self.charged_last_cycle),
            "paid_last_cycle": _encode(self.paid_last_cycle),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SpendSnapshot:
        if not isinstance(raw, dict):
            raise ArtifactError("spend is not a JSON object")
        try:
            return cls(
                statement_balance=_decimal(raw["statement_balance"], "statement_balance"),
                statement_due=date.fromisoformat(raw["statement_due"]),
                unbilled_balance=_decimal(raw["unbilled_balance"], "unbilled_balance"),
                unbilled_due=date.fromisoformat(raw["unbilled_due"]),
                reserved=_decimal(raw["reserved"], "reserved"),
                rolling_30d_cash=tuple(
                    _decimal(v, "rolling_30d_cash") for v in raw["rolling_30d_cash"]
                ),
                rolling_30d_card=tuple(
                    _decimal(v, "rolling_30d_card") for v in raw["rolling_30d_card"]
                ),
                charged_last_cycle=_decimal(raw["charged_last_cycle"], "charged_last_cycle"),
                paid_last_cycle=_decimal(raw["paid_last_cycle"], "paid_last_cycle"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError(f"malformed spend: {exc}") from exc


@dataclass(frozen=True)
class Artifact:
    """The whole served window, plus the stats derived from it."""

    version: int
    window_start: date
    window_end: date
    days: tuple[DayRecord, ...]
    summary: Summary
    spend: SpendSnapshot

    def by_day(self, day: date) -> DayRecord | None:
        """The record for `day`, or None — the single source of "no record".

        A date before the window, after it, or excluded as a warm-up day are all the same
        answer here, deliberately: the assistant's tools (U4) hand the user one uniform
        "no record" response rather than leaking which of the three it was.
        """
        for record in self.days:
            if record.day == day:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "summary": self.summary.to_dict(),
            "spend": self.spend.to_dict(),
            "days": [d.to_dict() for d in self.days],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Artifact:
        if not isinstance(raw, dict):
            raise ArtifactError("artifact is not a JSON object")

        try:
            version = int(raw["version"])
            window = raw["window"]
            artifact = cls(
                version=version,
                window_start=date.fromisoformat(window["start"]),
                window_end=date.fromisoformat(window["end"]),
                days=tuple(DayRecord.from_dict(d) for d in raw["days"]),
                summary=Summary.from_dict(raw["summary"]),
                spend=SpendSnapshot.from_dict(raw["spend"]),
            )
        except ArtifactError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError(f"malformed artifact: {exc}") from exc

        validate(artifact)
        return artifact


def validate(artifact: Artifact) -> None:
    """Everything the service assumes about the artifact, asserted in one place.

    Called on every decode, so a hand-edited or stale file fails at startup with a
    specific complaint rather than surfacing as a strange number in the UI later.
    """
    if artifact.version != SCHEMA_VERSION:
        raise ArtifactError(
            f"artifact schema version {artifact.version} != expected {SCHEMA_VERSION}"
        )

    if not artifact.days:
        raise ArtifactError("artifact has no days")

    days = [r.day for r in artifact.days]
    if days != sorted(days):
        raise ArtifactError("days are not in chronological order")
    if len(set(days)) != len(days):
        raise ArtifactError("days contain a duplicate date")

    if days[0] != artifact.window_start or days[-1] != artifact.window_end:
        raise ArtifactError(
            f"window {artifact.window_start}..{artifact.window_end} does not match the "
            f"served days {days[0]}..{days[-1]}"
        )

    for record in artifact.days:
        for name, amount in (
            ("decision.amount", record.decision.amount),
            ("checking_balance", record.checking_balance),
            ("buffer_floor", record.buffer_floor),
            ("debt_balance", record.debt_balance),
        ):
            if amount != amount.quantize(CENTS):
                raise ArtifactError(f"{record.day}: {name}={amount} is not quantized to cents")

        if record.decision.action is Action.SWEEP and record.decision.amount <= ZERO:
            raise ArtifactError(f"{record.day}: a sweep of {record.decision.amount} is not a sweep")
        if record.decision.action is Action.REFUSE and record.decision.amount != ZERO:
            raise ArtifactError(
                f"{record.day}: a refusal carries a non-zero amount {record.decision.amount}"
            )
        if not record.decision.reasons:
            raise ArtifactError(f"{record.day}: decision has no reasons")

    # The demo's whole point is that the engine both acts and declines to act. An artifact
    # that lost one of the two is a broken demo, and it should not be servable at all.
    actions = {r.decision.action for r in artifact.days}
    if Action.SWEEP not in actions:
        raise ArtifactError("served window contains no SWEEP day")
    if Action.REFUSE not in actions:
        raise ArtifactError("served window contains no REFUSE day")


def to_json(artifact: Artifact) -> str:
    return json.dumps(artifact.to_dict(), indent=2, sort_keys=True) + "\n"


def from_json(text: str) -> Artifact:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"artifact is not valid JSON: {exc}") from exc
    return Artifact.from_dict(raw)


def dump(artifact: Artifact, path: Path | None = None) -> None:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(artifact))


def load(path: Path | None = None) -> Artifact:
    """Read and validate the artifact. Raises `ArtifactError` on anything wrong with it.

    The default resolves at call time, not at import time — `path: Path = DEFAULT_PATH` would
    bind the module constant into the function's defaults and quietly ignore anyone who
    pointed `DEFAULT_PATH` somewhere else.
    """
    path = path or DEFAULT_PATH
    try:
        text = path.read_text()
    except OSError as exc:
        raise ArtifactError(f"cannot read artifact at {path}: {exc}") from exc
    return from_json(text)
