"""Recurring-stream detection — the income and the fixed bills, found in a transaction history.

The named-but-unbuilt piece between raw `plaid_transactions` rows and a decision (`architecture.md`,
`prd.md`: "the recurring-income detector we have not built"). The engine forecasts against a
`HouseholdSpec` — a payroll cadence, a set of scheduled bills — and a real linked household arrives
as a flat list of dated, signed, labelled movements with none of that structure. This is the pure
function that recovers it: group the movements by merchant, find the ones that *recur* on a regular
cadence with a stable amount, and return them as typed `RecurringStream`s a spec-builder can turn
into `PayrollSpec` / `BillSpec`.

**Deterministic and pure**, on purpose and for the same reason `assemble_snapshot` is: it takes a
list in and returns a list out, reads no clock and no database, so it is trivially testable and a
backtest grades the same detector production runs. It decides *nothing* about money — it only
describes what has been happening.

**Sign convention.** `Movement.amount` is the natural accounting sign: **positive is money in**
(a credit — payroll, a refund), **negative is money out** (a debit — rent, a card payment). Plaid's
own convention is the opposite (positive is an outflow from a depository account), so the adapter
that reads `plaid_transactions` flips the sign on the way in. Keeping the natural sign here means
`direction == "inflow"` reads as it says.

**What this is not.** It is a first, honest cut: a frequency/amount-stability heuristic, not a
learned model. It finds clean, regular streams (a biweekly paycheck, a monthly rent) and is
deliberately conservative — an irregular or one-off movement is left undetected rather than guessed
into a bill the forecast would then defend. `confidence` is reported, never hidden, so a caller can
raise the bar. Everything it misses falls through to discretionary spend, which is the safe default.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

ZERO = Decimal("0")

# A stream must recur at least this many times before we will call it recurring. Three is the
# smallest count that shows a *cadence* rather than a coincidence: two points make an interval, a
# third confirms the interval repeats.
MIN_OCCURRENCES = 3

# Below this we do not report the stream. A caller that wants only near-certain streams raises it.
MIN_CONFIDENCE = 0.5

# Cadence classification windows, in days between consecutive occurrences (median gap). The bands
# are deliberately loose — real statements land a day early on a weekend, a month is 28–31 days.
_WEEKLY = (6, 8)
_BIWEEKLY = (12, 16)
_MONTHLY = (26, 35)
# Biweekly and semimonthly share a ~14–16 day median gap; they are told apart by gap *evenness*.
# A biweekly cadence is metronomic (coefficient of variation ≈ 0); a semimonthly one alternates a
# short and a long gap (13 then 18), so its gaps vary well above this. Below it → biweekly.
_BIWEEKLY_GAP_CV = 0.10


@dataclass(frozen=True)
class Movement:
    """One dated, signed, labelled movement of money — the detector's input row.

    `amount` is the natural accounting sign (positive in, negative out — see the module docstring).
    `name` is the merchant/label as it arrived; normalization happens inside the detector.
    """

    day: date
    amount: Decimal
    name: str


@dataclass(frozen=True)
class RecurringStream:
    """A recurring income or expense recovered from the history.

    `typical_amount` is always **positive** (a magnitude); `direction` carries the sign's meaning.
    `day_of_month` is the stream's dominant calendar day — what a `BillSpec.day_of_month` or a
    payroll anchor keys on. `confidence` is 0..1 and combines how many times it recurred, how
    regular the cadence was, and how stable the amount was.
    """

    name: str
    direction: str  # "inflow" | "outflow"
    cadence: str  # "weekly" | "biweekly" | "semimonthly" | "monthly"
    typical_amount: Decimal
    day_of_month: int
    last_seen: date
    occurrences: int
    confidence: float


_NON_ALPHA = re.compile(r"[^a-z ]+")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """A stable merchant key: lowercased, digits and punctuation stripped, whitespace collapsed.

    "SQ *BLUE BOTTLE #4412" and "Sq Blue Bottle 5501" collapse to the same key, so two runs of the
    same recurring charge group together despite the store number and reference id that ride along
    on each. Empty after stripping (a purely numeric label) falls back to the original, trimmed —
    better a coarse key than none.
    """
    stripped = _SPACES.sub(" ", _NON_ALPHA.sub(" ", name.lower())).strip()
    return stripped or name.strip()


def _classify_cadence(gaps: list[float]) -> str | None:
    """The cadence a stream's inter-arrival `gaps` imply, or None if they fit no clean band.

    Weekly and monthly are read straight off the median gap. Biweekly and semimonthly *share* a
    ~14–16 day median and are told apart by the **shape** of the gaps, not their size: a biweekly
    paycheck lands every 14 days like a metronome (near-zero gap variance), while a semimonthly one
    (the 15th and the last day) alternates a short gap and a long one — 13 then 18 — so its gaps
    are markedly uneven. That is a more stable signal than the per-month *rate*, which a 70-day
    window of six biweekly paychecks inflates past two-a-month and misreads.
    """
    median_gap = statistics.median(gaps)
    if _WEEKLY[0] <= median_gap <= _WEEKLY[1]:
        return "weekly"
    if _BIWEEKLY[0] <= median_gap <= _BIWEEKLY[1]:
        return "biweekly" if _cv(gaps) < _BIWEEKLY_GAP_CV else "semimonthly"
    if _MONTHLY[0] <= median_gap <= _MONTHLY[1]:
        return "monthly"
    return None


def _cv(values: list[float]) -> float:
    """Coefficient of variation (stdev / mean), or 0 for a constant/degenerate series."""
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return statistics.pstdev(values) / abs(mean)


def _confidence(occurrences: int, gaps: list[float], amounts: list[float]) -> float:
    """Blend three independent signals into a 0..1 score.

    - **count** — more repetitions is more evidence, saturating at six (a half-year of a monthly
      bill, or six paychecks); beyond that, extra repetitions do not make it *more* recurring.
    - **regularity** — how even the gaps are (1 − their coefficient of variation): a metronomic
      cadence scores high, a ragged one low.
    - **stability** — how constant the amount is (1 − its coefficient of variation): rent is the
      same number every month; a "recurring" charge that swings wildly is probably not one.

    Weighted toward regularity, because a *regular cadence* is what makes a stream forecastable —
    an amount that drifts is still a schedulable event, but a cadence that drifts is not.
    """
    count = min(occurrences / 6.0, 1.0)
    regularity = 1.0 - min(_cv(gaps), 1.0)
    stability = 1.0 - min(_cv(amounts), 1.0)
    return round(0.30 * count + 0.45 * regularity + 0.25 * stability, 3)


def _stream(name: str, movements: list[Movement]) -> RecurringStream | None:
    """One candidate group → a `RecurringStream`, or None if it does not recur cleanly enough."""
    if len(movements) < MIN_OCCURRENCES:
        return None

    ordered = sorted(movements, key=lambda m: m.day)
    gaps = [float((b.day - a.day).days) for a, b in zip(ordered, ordered[1:], strict=False)]
    if not gaps or min(gaps) <= 0:
        # Two movements on the same day are not a cadence; a zero gap would also divide by nothing.
        return None

    cadence = _classify_cadence(gaps)
    if cadence is None:
        return None

    magnitudes = [float(abs(m.amount)) for m in ordered]
    confidence = _confidence(len(ordered), gaps, magnitudes)
    if confidence < MIN_CONFIDENCE:
        return None

    return RecurringStream(
        name=name,
        direction="inflow"
        if statistics.fmean([float(m.amount) for m in ordered]) > 0
        else "outflow",
        cadence=cadence,
        typical_amount=abs(_median_amount(ordered)),
        # The dominant calendar day — the one it lands on most often. A stream that wanders a day
        # each month still has a clear mode, which is what a monthly schedule anchors to.
        day_of_month=Counter(m.day.day for m in ordered).most_common(1)[0][0],
        last_seen=ordered[-1].day,
        occurrences=len(ordered),
        confidence=confidence,
    )


def _median_amount(movements: list[Movement]) -> Decimal:
    """Median signed amount, as a `Decimal` (kept exact — never routed through float)."""
    amounts = sorted(m.amount for m in movements)
    mid = len(amounts) // 2
    if len(amounts) % 2 == 1:
        return amounts[mid]
    return (amounts[mid - 1] + amounts[mid]) / 2


def detect_recurring(movements: list[Movement]) -> list[RecurringStream]:
    """Every recurring stream in `movements`, most-confident first.

    Movements are grouped by `(normalized name, sign)` — a merchant that both charges and refunds
    is two streams, not one that cancels out — and each group is tested for a clean cadence and a
    stable amount. A group that recurs too few times, on no recognizable cadence, or with a low
    confidence is dropped: undetected, not guessed. The result is sorted by confidence so a caller
    taking "the paycheck" can take the highest-confidence inflow.
    """
    groups: dict[tuple[str, bool], list[Movement]] = {}
    for m in movements:
        if m.amount == ZERO:
            continue  # a zero movement carries no cadence and no amount signal
        key = (normalize_name(m.name), m.amount > ZERO)
        groups.setdefault(key, []).append(m)

    streams = [s for (name, _sign), group in groups.items() if (s := _stream(name, group))]
    return sorted(streams, key=lambda s: s.confidence, reverse=True)
