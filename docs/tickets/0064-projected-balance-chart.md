---
id: "0064"
title: A projected-balance chart — the forecast the whole engine runs on, made visible
type: feat
status: open
priority: medium
repo: cfo-ai
agentId: backend-python-agent
agentKind: classic
agentScope: repo
created: 2026-07-22
---

# A projected-balance chart

Every decision the engine makes turns on one projection: *where is this household's checking balance
headed, and how low does it get?* `conservative_low_balance` walks the next 30 days day by day and
finds the low, and that low is what licenses or refuses a sweep. Today that projection reaches the
customer as **one sentence** in the explain modal ("Your cash balance will be $1,291.47 on 2026-06-05
— the lowest it gets, after your $800.00 buffer"). This ticket draws it.

It is the one chart that pays for itself: it shows the engine's actual reasoning, and it answers the
persona's real question (*am I going to be OK*) better than any spending breakdown could.

## The data reality — this decides the scope

- **The forecast already computes the whole curve.** `engine/forecast.py:conservative_low_balance`
  loops `for offset in range(HORIZON_DAYS + 1)` accumulating `balance`, and keeps only `(low,
  low_day)`. The 31 daily points exist; they are thrown away. Returning them is cheap.
- **There is no *historical* balance to plot.** No `transactions` table until ingest lands
  (`architecture.md [3.1]` — the same wall the income buckets and the spend history hit). So v1 is
  **projection-forward only**: today → the 30-day horizon. The "actual balance, trailing" half waits
  on ingest and is explicitly out of scope here.
- **The projection is the *conservative worst case*** — it takes the worse end of every event
  (`forecast.py`). The chart must be framed as that, or it alarms.

## Approach

### 1. Engine — return the trajectory (`engine/forecast.py`)

Add `balance_trajectory(snapshot) -> tuple[tuple[date, Decimal], ...]`: the `(day, balance)` point
for each `offset in 0..HORIZON_DAYS`. Refactor `conservative_low_balance` to derive its result from
that trajectory (`min` of the points) so there is **one walk and no second copy to drift** — the
oracle stays exact. Pure, no new inputs.

### 2. Backend — derive at serve time, never store (`backend/spend.py` / decision serialization)

The trajectory is a *derivable fact of the snapshot*, so it is computed on read, not persisted — the
same rule the reserve/obligations half follows ("a stored copy of a derivable fact is a copy that
can disagree", `spend.py`). Serve it on the decision, both paths:

- seeded households: recompute from the **stored snapshot** for the served day;
- linked households: from the live snapshot `/live-decision` already assembles.

Wire shape (a `projection` object on the decision):

```
projection: {
  as_of:        "2026-05-21",
  horizon_end:  "2026-06-20",
  buffer_floor: "800.00",
  low:          "1291.47",
  low_day:      "2026-06-05",
  points: [ { day: "2026-05-21", balance: "4174.56" }, … 31 points … ]
}
```

### 3. API types (`mobile/src/api/types.ts`)

A `BalanceProjection` type matching the above.

### 4. Mobile — a calm line/area chart in the ExplainModal (`react-native-svg`)

A projected-balance **line**, a **buffer-floor reference line** under it, the **low point** marked,
and the cushion (the gap between the line and the floor) as a quiet fill — rendered with
`react-native-svg` (decision 2, above), inside `ExplainModal` beside the projection reason (decision
1). **No red** — a household whose line dips toward the floor is being shown the truth, not a fault
(`theme.ts` has no red on purpose). `Spending.tsx`'s `StripChart` (hand-rolled `View`s) stays as-is;
this is the first chart that earns a real charting primitive.

## The chart, specified

- **X axis:** time, today → `horizon_end` (31 daily points). No dense date labels — endpoints only
  ("today" … the horizon date), matching the app's restraint.
- **Y axis:** dollars. The **buffer floor** drawn as a horizontal reference line, labelled with its
  amount. No gridlines.
- **The line:** the projected balance. Smooth, single colour (`brandBlue`, as `StripChart` uses for
  its marked bar).
- **The low point:** a dot at `(low_day, low)`, labelled — this is the number the sentence already
  quotes, now located in time.
- **The cushion:** a faint fill between the line and the floor — "this is your room". Calm, not a
  warning band.

## Copy / framing (load-bearing)

The chart title and caption carry the same care the explain sentence does:

- title: **"Where your cash is headed"**
- caption: **"The lowest we expect over the next 30 days, worst case — after your $800 buffer. We
  plan against the bad version, so the good one takes care of itself."**

The story the reader should take is *"the line stays above my floor"*, never *"look how low it
dips"*. On a refusal day where the projected low dips **below** the floor, the caption flips to name
the dip as the reason for the hold — *"Your cash gets tight around <low_day>, so we're leaving it
alone"* — still no red, still reassurance (see decision 4).

## Scope & non-goals

- **In:** the forward 30-day projected-balance curve, floor line, low point, calm framing; both
  household types; derived-not-stored.
- **Out (v1):** trailing *actual* balance history (blocked by ingest); a **projected-spending** line
  on the Spending chart (the forward spend model is a flat p90 rate already baked into this
  projection — a flat line adds noise, per the 2026-07-22 discussion); any new *claim* — the chart
  says nothing the projection sentence does not already say.

## Decisions (settled 2026-07-22)

1. **Placement — `ExplainModal`, v1.** The chart goes beside the projection sentence it illustrates.
   Lowest design risk, per-decision, and the chart and the text reinforce each other. A standing
   Dashboard "runway" panel is a possible fast-follow once the tone is proven — **not** in this
   ticket.
2. **Chart tech — add `react-native-svg`.** A real line + area + floor marker is where hand-rolling
   from `View`s stops paying off; svg gives clean lines and fills and will be reused for the next
   chart, so the dependency is justified. **Before adding it**, read the exact Expo v57 docs
   (`mobile/AGENTS.md`) and pin the `react-native-svg` version Expo SDK 57 expects — a mismatched
   native module is the failure mode here.
3. **Floor line — `buffer_floor` only.** Draw just the safety buffer as the floor line; mention the
   reserved card obligations in the caption rather than drawing a second line. The projected line is
   the raw balance. (`buffer_floor + reserved` was considered and rejected as two concepts too many
   for the space.)
4. **The below-floor case — show it, framed as protection.** When the projected low dips under the
   floor (a tight-cash refusal day), draw the honest dip — **no red** — and let the copy name it as
   the reason we held back: *"Your cash gets tight around <low_day>, so we're leaving it alone."* The
   dip **is** the reason for the refusal, so showing it is truthful and the framing makes it
   reassurance ("we saw this and protected you"), not alarm. This is only reachable on refusal days;
   a sweep never pushes the projected low below the floor by construction (`available` is what is
   left *above* buffer + reserved).

## Acceptance criteria

- [ ] `balance_trajectory` returns 31 daily points; `min(points) == conservative_low_balance().low`
      is pinned by a test, so the chart and the decision can never disagree about the low.
- [ ] The projection is derived from the snapshot at serve time and **never written to the DB**.
- [ ] `/decisions` (seeded) and `/live-decision` (linked) both serve the `projection` object.
- [ ] The chart renders in the `ExplainModal` (decision 1) via `react-native-svg` (decision 2, at the
      Expo-SDK-57-pinned version), showing the line, the `buffer_floor` line, and the low point, with
      **no red**.
- [ ] The below-floor case shows the honest dip with the protection framing (decision 4), not an
      alarm; verified on a refusal day whose projected low is under the floor.
- [ ] The chart makes **no claim** the projection sentence does not already make; an unreachable or
      empty projection degrades to the existing text, never a broken chart.

## Tests

- Engine: `min(trajectory) == low`; `len == HORIZON_DAYS + 1`; `points[0].day == snapshot.today`;
  a household with a mid-horizon dip puts `low_day` at the dip, not the endpoint.
- Backend: the `projection` object serializes on both decision paths; not persisted (no new table /
  column written).
- Mobile: renders N points + the floor + the low marker; an empty/degenerate projection falls back
  to text without crashing (mirror `StripChart`'s empty-series test).

## Risks

- **Tone.** This is the first chart to visualize a *decision*, not history. Done carelessly it turns
  a reassurance app into an anxiety one — the framing questions above are not decoration.
- **Bigger than it looks.** Engine + API + mobile + possibly a new dependency, and it touches the
  deployed customer app — not a copy change. Worth prototyping the backend curve behind a flag before
  committing the mobile surface.
