# RUNBOOK — the calibration measurement (`0018` / U9)

How to run the measurement that decided the engine's spend model, what to look at, and —
importantly — **what not to expect to see.**

> ## Read this first
>
> **This has almost no UI surface.** The spend model `0018` built is shipped **inert**: the dial is
> off, `forecast.py` still uses the old model, and **not one household decision changes.** If you
> open the app looking for the feature, you will not find it, and that is the work behaving as
> intended.
>
> The deliverable is a **measurement** — the thing that decided *not* to ship the model. The most
> important "page" here is a terminal output (`python -m backend.calibrate`) and a learning doc,
> not a screen.

> **Written to review PR #31, which merged on 2026-07-14.** Kept because the measurement outlived
> the review: **2.3% breach / 0 sweep-caused overdrafts / $544,640.58** is cited by `prd.md`,
> `strategy.md`, and `decision-engine.md`, and this file is the only walkthrough of where those
> numbers come from. The review-specific framing below is historical; the procedure is current.

---

## 0. Setup (once)

Setup now lives in [`runbooks/local-development.md`](./runbooks/local-development.md) — **use the
venv**, and note the database suites need a real Postgres:

```bash
cd ~/workspace/cfo-ai
python3 -m venv .venv                  # if it does not exist
.venv/bin/pip install -e ".[dev]"      # pytest, ruff, httpx + the API extras (fastapi, uvicorn)
```

`engine/` and `sim/` have **zero dependencies** on purpose. Everything installed above is for the
web shell and the test tools.

> The original said `git checkout feat/spend-model-calibration` and `python3 -m pip install`. That
> branch merged, and system `pip` is now wrong — see `local-development.md` for why the venv is not
> optional. This is `main`.

---

## 1. The 60-second check

```bash
.venv/bin/python -m pytest tests/            # 393 passed, 139 skipped, ~48s — no database
.venv/bin/python -m ruff check backend/ engine/ tests/
.venv/bin/python -m ruff format --check backend/ tests/
```

**Expected:** 393 passed / 139 skipped, "All checks passed!", "39 files already formatted".
Measured 2026-07-17.

> **The 139 skips are the database suites**, and they are why this is a 60-second check rather than
> a real one. With `TEST_DATABASE_URL` set (see
> [`runbooks/local-development.md`](./runbooks/local-development.md)) it is **531 passed, 1
> skipped**. This block used to say `python3 -m pytest` and "367 passed" — system Python silently
> skips every database test, and 367 predates `0020`.

`mypy` reports **12 pre-existing errors in `engine/decide.py`** (`Card | None` union-attr). They are
on `main` too — confirm with `git stash && python3 -m mypy backend/replay.py`. Nothing in this PR's
files errors.

---

## 2. The actual deliverable — run the calibration

**This is the review.** Everything else is supporting material.

```bash
python3 -m backend.calibrate            # ~90 seconds, 60 households x 9 dial settings
```

**Expected output:**

```
  dial      graded  breach%  worst-shape%  overdrafts  false-refusal cost  licensed
  ------------------------------------------------------------------------------------
  today       4320    2.3%         5.8%           0         $544,640.58
  q=3         4320    0.5%         1.2%           0       $1,622,656.36  NO
  q=2         4320    1.6%         3.2%           1       $1,015,620.12  NO
  q=1.5       4320    3.8%         6.2%           0         $699,237.92  NO
  q=1.25      4320    8.0%        10.7%           1         $566,549.12  NO
  q=1         4320   19.8%        22.0%           1         $380,499.57  NO
  ...
  No setting is licensed. The dial stays where it is.
```

### The three things to actually look at in that table

1. **`today` breaches 2.3%. `q=1.0` breaches 19.8%.** `q=1.0` means *reserving against the worst
   30-day stretch this household has ever actually had* — and it is still optimistic on one day in
   five. That is the finding that killed the swap.
2. **`q=3` says `NO` even though it looks best.** 0.5% breach, zero overdrafts — and it is refused,
   because it costs **$1.62M** in false-refusal cost against today's **$544K**. Reserving more
   always breaches less, so a safety bar that only measures safety will license the most
   conservative, least useful thing you show it. That third condition in `licensed()` is the fix,
   and this row is the reason it exists.
3. **The overdraft column flickers** (`1, 0, 1, 1, 1, 0`) between adjacent settings. Don't read that
   as signal — a sweep changes the walk, so trajectories diverge chaotically at n=60. What *is*
   signal: **`today` is `0` in every run.**

### Optional: reproduce the load-bearing experiment (~5 min)

The claim that the model is *starved* rather than *wrong* rests on one experiment — give it three
years of history instead of 60 days and the breach rate collapses.

```bash
python3 - <<'EOF'
from backend.calibrate import SHAPES, _spec_for
from backend.replay import replay, calibrate
for warmup, label in ((60, "60d history (what the engine has)"), (365*3, "3y history")):
    print(f"\n=== {label} ===")
    for name, shape in SHAPES.items():
        spec = _spec_for(shape)
        g = [x for s in range(11, 19) for x in replay(spec=spec, seed=s, spend_quantile=1.0, warmup=warmup)]
        c = calibrate(g)
        print(f"  {name:<15} breach at q=1.0: {c.breach_rate:6.1%}   overdrafts: {c.sweep_caused_overdrafts}")
EOF
```

**Expected:** breach at `q=1.0` falls from ~19% to ~4% overall, and the **high-variance household
goes to 0.0%** — the one it is most dangerous for today. Nothing changed but the history.

---

## 3. Run the app (optional — you will see no new feature)

### Backend

```bash
export RESFI_API_KEY=dev-key
export ANTHROPIC_API_KEY=sk-dummy      # required at startup; a dummy is fine unless you use the assistant
export DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:55432/cfo_ai"
python3 -m uvicorn backend.main:app --reload --port 8000
```

`ANTHROPIC_API_KEY` is **not optional** — `backend/main.py`'s lifespan builds the assistant client
and the app refuses to boot without it. A dummy value works for every endpoint except
`/assistant/message` and the `explain` route, which actually call Anthropic.

**`DATABASE_URL` is not optional either, and the households have to be seeded.** Since tickets
`0024` and `0031` every route reads Postgres — the committed artifact is a test fixture and nothing
serves it. The app refuses to start on an unreachable or unmigrated database, and *also* on a role
that bypasses RLS (Neon's default role does; so does a local superuser that owns the tables). See
[`runbooks/local-development.md`](./runbooks/local-development.md) for standing one up, then:

```bash
python3 -m alembic upgrade head
python3 -c "from backend.db.session import make_engine; from backend.seed import seed_all; print(seed_all(make_engine()))"
```

Check it:

```bash
H=hh_demo_biweekly
curl -s localhost:8000/health                                          # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/households      # 401 — auth is on
curl -s -H "X-API-Key: dev-key" localhost:8000/households | python3 -m json.tool
curl -s -H "X-API-Key: dev-key" "localhost:8000/households/$H/decisions" | python3 -m json.tool | head -20
curl -s -H "X-API-Key: dev-key" "localhost:8000/households/$H/spend" | python3 -m json.tool

# The portfolio household — three cards, each with its own panel. This is what ticket 0031
# fixed: the old `GET /spend` reported one arbitrary card as "your card", for one household.
curl -s -H "X-API-Key: dev-key" localhost:8000/households/hh_semimonthly_portfolio/spend \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['totals']); [print(c['card_id'], c['this_cycle']['held_back']) for c in d['cards']]"
```

### Mobile

```bash
cd mobile && npm install
EXPO_PUBLIC_API_URL=http://localhost:8000 EXPO_PUBLIC_API_KEY=dev-key npm run web
```

Two screens: **Decisions** (the feed) and **Spending**. Both should look **exactly as they did on
`main`.** If either looks different, that is a bug in this PR, not a feature of it.

```bash
cd mobile && npm test && npm run typecheck
```

### Regenerate the artifact

```bash
python3 -m backend.precompute        # rewrites backend/data/decisions.json
git diff --stat backend/data/decisions.json   # should be EMPTY — the committed artifact is current
```

---

## 4. Verify the one thing that *did* change

I claimed in the PR that no decision changes. **Check it rather than trusting it** — and note the
artifact is *not* byte-identical to `main`, because the payday double-count fix moves the forecast.

```bash
git show main:backend/data/decisions.json > /tmp/main-decisions.json
python3 - <<'EOF'
import json
a = json.load(open('/tmp/main-decisions.json')); b = json.load(open('backend/data/decisions.json'))
da = {x["day"]: x for x in a["days"]}; db = {x["day"]: x for x in b["days"]}
actions = [d for d in da if da[d].get("action") != db[d].get("action")]
print("days whose ACTION changed:", len(actions))          # 0
print("days whose projection changed:", sum(json.dumps(da[d],sort_keys=True) != json.dumps(db[d],sort_keys=True) for d in da))  # 41
for l, d in (("main", a), ("branch", b)):
    print(f"{l:<7} {d['summary']['sweep_count']} sweeps / {d['summary']['refuse_count']} refusals, swept {d['summary']['total_swept']}")
EOF
```

**Expected:** **0** actions change. 41 of 90 projections change. Still 10 sweeps / 80 refusals; total
swept drops **$11,719.23 → $11,705.45**.

Sweeps get *slightly smaller*. That is the correct direction: the forecast stopped counting a
paycheck that had already landed as though it were still coming, so it projects a slightly lower low
and holds back slightly more. **The guardrail working, not a regression.**

---

## 5. What to review, in priority order

| # | Page | Why |
|---|---|---|
| **1** | [`docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`](learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md) | **The PR is this document.** The measurement, the wrong explanation I nearly shipped, the starvation proof, and the three honest options. If you read one thing, read this. |
| **2** | `backend/calibrate.py` — `licensed()` | The three-condition rule, and why the missing third condition made the harness license regressions. This is the only *behavioural* bug fixed in code this PR. |
| **3** | `backend/precompute.py` — `derive_cash_events` | The payday double-count. `Snapshot.balance` is end-of-today, so emitting today's events as *future* ones counted the paycheck twice. 43 sweep-caused overdrafts on the population; **zero on the demo household**. |
| **4** | `tests/test_calibrate.py` | Especially `test_the_empirical_swap_is_not_licensed_as_the_learning_specified_it` — its docstring is where the wrong "mid-horizon amortization" explanation lived. Check I replaced it with something true. |
| **5** | `backend/precompute.py` — `spend_30d_high` + `engine/forecast.py` — `_spend_per_day` | The model itself, and the `None` fallback that makes it inert. |
| **6** | `docs/tickets/0018-spend-model-calibration.md`, `docs/implementation-notes.md` | The ticket and the running notes. |

### The claims worth attacking

- **"Both models are flat, so amortization can't explain the gap."** `_spend_per_day` returns a
  constant; the old path used a constant. If that's wrong, my whole diagnosis is wrong.
- **"The bias is worst for the fat-tailed household."** 58% of its true worst month. That's the
  claim that makes this dangerous rather than merely inaccurate.
- **"Reserving more always breaches less."** The monotonicity argument behind the `licensed()` fix.
- **The population is 60 simulated households, 3 shapes, 20 seeds.** It is a demo population, not a
  prior. Every number here inherits that.

---

## 6. What is deliberately NOT in this PR

- **The over-reserve is still unfixed.** `30 × p90_daily` still over-reserves by $400–970. We now
  know the *proposed fix is unavailable at the history the engine has*.
- **No history-gated model.** The most promising direction — the empirical model beats the incumbent
  for households with years of data — is a second model with an eligibility rule, not a dial. It
  wants its own ticket.
- **No dial in production.** `SPEND_QUANTILE = None`, pinned by a test that fails if anyone moves it
  without a measurement.
