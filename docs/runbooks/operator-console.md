---
title: Running the operator console
last-verified: 2026-07-22
anchor: RB-operator-console
---

# Running the operator console

The support surface from the Admin lane of `docs/status.html`: **look up a household, read the exact
snapshot behind any decision, and pause sweeps for one account or all of them.** It is a standalone
app (`backend/operator.py`), deliberately separate from the customer app — its own port, its own
audience, its own auth.

> ## Read this first
>
> **The auth is a god-mode placeholder, and it is not safe to expose.** There is no operator identity
> yet (membership is `owner`/`viewer` only; `status.html` marks the operator half "still to come").
> Until it exists, the console is gated by **one** username + password from the environment. That is
> enough to keep a shoulder-surfer out of a tool running on `127.0.0.1`; it is **not** enough to put
> on the public internet. **Run it locally. Do not deploy it `--allow-unauthenticated`.** Real
> per-operator auth + an access log is the `Access & audit` box, still to come.
>
> **It is read-mostly, and one write is not yet enforced.** Lookup and the snapshot trace are reads.
> *Pause* is a real write that changes decisions (see `[5]`). The *global halt* is recorded and
> displayed but its **enforcement is a deliberate stub** — nothing moves money today (the transfer
> leg is shadow), so `assert_not_halted()` is a hook the sweep-execution rung will call when that
> goes live, not a guard already on a live path (see `[7]`).

---

## [1] Configure

Two things are required; the rest have safe defaults.

| Env var | Required | Default | What it is |
|---|---|---|---|
| `OPERATOR_PASSWORD` | **yes** | — | The god-mode password. The console **refuses to start without it** (fail-loud, like the API without `DATABASE_URL`). |
| `DATABASE_URL` | **yes** | — | The database to operate on. Local test DB, or Neon. Same value the API uses. |
| `OPERATOR_USER` | no | `operator` | The god-mode username. |
| `OPERATOR_SESSION_SECRET` | no | random per process | HMAC key for the session cookie. Omit and a restart simply asks you to log in again — the safe default for a single-process local tool. Set it to keep sessions across restarts. |
| `OPERATOR_PORT` | no | `8787` | Port to bind on `127.0.0.1`. |

Pick a password you control — **never commit one**. For a local run against the throwaway Postgres
from [`local-development.md`](./local-development.md):

```bash
export DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:55432/cfo_ai_test"
export OPERATOR_PASSWORD='<something only you know>'
```

The console reads households from whatever `DATABASE_URL` points at. If it is empty (fresh test DB),
seed the four archetypes first: `.venv/bin/python -c "from backend.db.session import make_engine;
from backend.seed import seed_all; seed_all(make_engine())"`.

## [2] Start it

```bash
.venv/bin/python -m backend.operator
```

It prints the URL and the operator name, then serves on `127.0.0.1`:

```
operator console → http://127.0.0.1:8787  (user: operator)
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
```

Open that URL. `Ctrl-C` stops it. If it exits immediately with `OPERATOR_PASSWORD is not set`, that
is [1] not done — the refusal is deliberate.

## [3] Sign in

The landing page is a login form (every other route redirects here until you have a session). Enter
the operator name (`operator` unless you set `OPERATOR_USER`) and `OPERATOR_PASSWORD`. A wrong
password returns to the form; a correct one drops a signed, HTTP-only cookie good for 8 hours and
lands you on the household list. **Sign out** is in the top-right.

## [4] Look up a household

The dashboard lists **every** household with, per row: its id, its label, a `demo` tag if it is a
demo household, an **active / paused** badge, and its last decision (`sweep`/`refuse` + date). Click
a row to open it. The household page shows its **policy** (buffer floor, max sweep, cadence, and the
paused window if any) and its **decisions**, newest first.

## [5] Read the exact snapshot behind a decision

Click any decision in the list. The right pane renders the **frozen snapshot the engine actually
saw**, replayed through `decide()` gate by gate: preconditions → coverage → forecast → target →
surplus (as an arithmetic line) → cadence → caps → interest → the outcome. Every value is the
engine's own — this is the same `backend/trace.py` behind the CEO decision panel, pinned to the
engine by `tests/test_trace.py`. A decision with no stored snapshot (older seeded rows may predate
the store) says so plainly rather than inventing one.

## [6] Pause / resume one household

On a household page, **Pause sweeps** blacks out the next 90 days by writing them into that
household's `policy_events.blackout_dates` — the same pause surface the customer's own Settings uses,
and the one `decide()` refuses on (`ReasonCode.BLACKOUT`). The badge flips to **paused** and the
policy shows the window (`2026-07-22 → 2026-10-19 (90 days)`). **Resume sweeps** drops every
blacked-out day from today forward; past days are left as history.

> **What a pause actually changes today.** A pause bites where a decision is computed *live* — a
> linked household re-decided through `GET /live-decision`. The four seeded demo households serve
> **precomputed** decisions, so a pause changes their state and any future live decision but **not**
> their historical feed. This is the same seeded-vs-live split the customer app has (`main.py`'s
> `PATCH /policy` note).

## [7] Halt / resume all money movement

On the dashboard, **Halt all** records a global halt; a red-free banner and a top strip then show
"Money movement halted" on every page until you **Resume**. The halt *state* is derived from the
latest halt/resume row in the audit log — there is no separate flag to drift.

> **The kill switch is recorded, not yet enforced.** `backend/operator.py:assert_not_halted()` is the
> guard the sweep-execution rung will call before moving money. It is **not wired into any live
> path** — deliberately: nothing moves money today (the transfer leg is shadow), and an operator flag
> does not belong in the customer/transfer code without a decision to put it there. So today a halt
> is an audited, visible intent + a ready hook, not a control that already stops a running transfer.
> Wiring it is a one-line `assert_not_halted(engine)` call at the execution rung when that goes live.

## The audit log

Every override — pause, unpause, halt, resume — is written to `operator_actions` with **who, what,
which household (or "all"), and when**, and shown in *Recent operator actions* on the dashboard. The
table is **append-only by grant**: the application role holds `SELECT`/`INSERT` and no `DELETE`, so an
override cannot be un-recorded after the fact (`tests/test_operator.py` asserts the `DELETE` is
refused). Nothing here can be edited from the console; a correction is another action.

---

## Config reference — quick copy

```bash
export DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:55432/cfo_ai_test"  # or Neon
export OPERATOR_PASSWORD='<your password>'      # required; refuses to start without it
export OPERATOR_USER='operator'                 # optional (default: operator)
export OPERATOR_SESSION_SECRET='<random hex>'   # optional; omit for random-per-process
export OPERATOR_PORT=8787                        # optional
.venv/bin/python -m backend.operator
```

## When something does not answer

| Symptom | What it is | What to do |
|---|---|---|
| Exits: `OPERATOR_PASSWORD is not set` | The fail-loud guard | Set `OPERATOR_PASSWORD` (`[1]`). |
| `0 households` on the dashboard | `DATABASE_URL` points at an empty DB | Wrong DB, or unseeded test DB — seed the archetypes (`[1]`). |
| Every page bounces to `/login` | No/expired session (8h), or a restart with no `OPERATOR_SESSION_SECRET` | Sign in again; set `OPERATOR_SESSION_SECRET` to survive restarts. |
| A decision shows "No snapshot stored" | The row predates the snapshot store | Expected for old rows — the decision is real, its frozen inputs just were not kept. |
| Pause did not change a demo household's feed | Seeded households serve precomputed decisions | Expected — see the note in `[6]`; pause bites a live-decided household. |
| Halt banner shows but a (future) transfer still runs | The kill switch is not yet enforced | Expected today — see `[7]`; enforcement is the documented stub. |

## Where it lives

- App + auth + rendering: `backend/operator.py` (stylesheet: `backend/operator.css`).
- Audit table: migration `alembic/versions/0016_operator_actions.py`.
- Tests: `tests/test_operator.py` (the gate, the trace fidelity, pause-really-refuses, halt-derived,
  append-only). Run: `.venv/bin/python -m pytest tests/test_operator.py`.
- It reuses `backend/trace.py` (the decision trace) and `backend/readpath.list_households` — no second
  read path into a household's data is invented; an operator sees exactly what that household's own
  session would, plus the frozen snapshot the customer app never exposes.
