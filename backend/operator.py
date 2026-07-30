"""The operator console — a support surface, strictly separate from the customer app.

The "Admin" lane of `docs/status.html`: *look up a household, read the exact snapshot behind any
decision, and pause sweeps for one account or all of them.* It is the operator's window into a live
money-moving service, and it is deliberately **not** part of the customer app — different port,
different auth, different audience (`USERS.md` §2).

## Auth is a placeholder, and says so

There is no operator identity yet — the membership graph has `owner` and `viewer`, and
`docs/status.html` marks the operator half of identity "still to come". Until it exists, the
console is gated by a single **god-mode** username + password from the environment (`OPERATOR_USER`,
`OPERATOR_PASSWORD`), verified in constant time, carried in an HMAC-signed session cookie. That is
enough to keep a shoulder-surfer out of a *local* tool; it is **not** enough to deploy on the public
internet, and the console refuses to start without a password precisely so the gap is loud rather
than silent. Real per-operator auth + an access log is the `Access & audit` box, still to come.

## Everything it reads, it reads through the same layers the customer app does

Household lookup is `readpath.list_households` (the tenant registry, unscoped by design). Everything
past an id — policy, decisions, the snapshot behind a decision — goes through the household-scoped
`Repository` and RLS, one household at a time. The console never invents a second read path into a
household's data; an operator who picks a household gets exactly what that household's own session
would, plus the frozen `Snapshot` the customer app never exposes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Engine, text

from backend import readpath
from backend.db.repository import repository
from backend.db.session import make_engine
from backend.db.snapshots import PostgresSnapshotStore, SnapshotStoreError
from backend.trace import BLOCK, PASS, DecisionTrace, GateStep, trace

# --- configuration -------------------------------------------------------------------

COOKIE = "op_session"
SESSION_TTL_SECONDS = 8 * 3600

THEME_COOKIE = "op_theme"
THEME_TTL_SECONDS = 365 * 24 * 3600
#: The only two values that may ever reach `<html data-theme=…>`. Absent means "Auto" — no
#: attribute at all, so `prefers-color-scheme` decides. Validated on the way in *and* on the way
#: out: the cookie is attacker-controllable in a way the session token is not (it is unsigned, so
#: that a preference survives a signing-secret rotation), and it is interpolated into an HTML
#: attribute. An allowlist is the only safe shape.
THEMES = ("light", "dark")


class OperatorNotConfigured(RuntimeError):
    """`OPERATOR_PASSWORD` is unset. Raised at startup, never per-request — the same
    fail-loud posture the API takes for `DATABASE_URL` and the customer auth keys."""


def _operator_user() -> str:
    return os.environ.get("OPERATOR_USER", "operator")


def _operator_password() -> str:
    pw = os.environ.get("OPERATOR_PASSWORD")
    if not pw:
        raise OperatorNotConfigured(
            "OPERATOR_PASSWORD is not set. The operator console is god-mode access to every "
            "household; it refuses to start without a password. Set OPERATOR_PASSWORD (and "
            "optionally OPERATOR_USER, OPERATOR_SESSION_SECRET) and try again."
        )
    return pw


# One signing secret per process. An explicit `OPERATOR_SESSION_SECRET` keeps sessions valid across
# restarts / multiple workers; without it, a fresh random secret means a restart simply asks the
# operator to log in again — the safe default for a single-process local tool.
_SIGNING_SECRET = (os.environ.get("OPERATOR_SESSION_SECRET") or secrets.token_hex(32)).encode()


def _sign(payload: str) -> str:
    sig = hmac.new(_SIGNING_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    body = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{body}.{sig}"


def _unsign(token: str) -> str | None:
    """The payload if the token is authentic and unexpired, else None."""
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(_SIGNING_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return payload


def _issue_session(user: str) -> str:
    return _sign(f"{user}|{int(time.time()) + SESSION_TTL_SECONDS}")


def _session_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    payload = _unsign(token)
    if payload is None:
        return None
    try:
        user, exp = payload.split("|", 1)
        if int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    return user


def _session_theme(request: Request) -> str | None:
    """The pinned theme, or `None` for Auto. Anything unrecognised is Auto."""
    theme = request.cookies.get(THEME_COOKIE)
    return theme if theme in THEMES else None


def _safe_next(target: str) -> str:
    """A local path to return to, or `/`.

    `next` arrives from a form field, so it is attacker-controllable via a crafted link. An
    operator console that forwards to an off-site page is a phishing primitive — the operator is
    one bounce from a convincing fake of this very login form, and this login form is god-mode.
    So: one leading slash, no second slash or backslash (`//host` and `/\\host` are both
    protocol-relative to a browser), and no scheme.
    """
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


def _check_login(user: str, password: str) -> bool:
    """Constant-time comparison of both halves, so neither leaks its length by timing."""
    user_ok = hmac.compare_digest(user, _operator_user())
    pw_ok = hmac.compare_digest(password, _operator_password())
    return user_ok and pw_ok


# --- data the console reads ----------------------------------------------------------


@dataclass(frozen=True)
class HouseholdStatus:
    id: str
    label: str
    archetype: str | None
    is_demo: bool
    paused: bool
    blackout_dates: list[str]
    last_action: str | None
    last_day: str | None


def _household_status(
    engine: Engine, household_id: str, label: str, archetype: str | None, is_demo: bool
) -> HouseholdStatus:
    with repository(engine, household_id) as repo:
        policy = repo.policy()
        blackout = [] if policy is None else [_iso(d) for d in policy.get("blackout_dates", [])]
        last = repo.last_decision()
    return HouseholdStatus(
        id=household_id,
        label=label,
        archetype=archetype,
        is_demo=is_demo,
        paused=bool(blackout),
        blackout_dates=blackout,
        last_action=None if last is None else str(last["action"]),
        last_day=None if last is None else _iso(last["day"]),
    )


def list_status(engine: Engine) -> list[HouseholdStatus]:
    """Every household, with just enough state to triage: paused?, and its last decision."""
    with engine.connect() as conn:
        households = readpath.list_households(conn)
    return [_household_status(engine, h.id, h.label, h.archetype, h.is_demo) for h in households]


@dataclass(frozen=True)
class DecisionRow:
    day: str
    action: str
    amount: str
    target: str | None


@dataclass(frozen=True)
class HouseholdDetail:
    status: HouseholdStatus
    buffer_floor: str | None
    max_sweep: str | None
    min_days_between_sweeps: int | None
    decisions: list[DecisionRow]


def household_detail(engine: Engine, household_id: str) -> HouseholdDetail | None:
    with engine.connect() as conn:
        found = readpath.list_households(conn, only=[household_id])
    if not found:
        return None
    h = found[0]
    status = _household_status(engine, h.id, h.label, h.archetype, h.is_demo)
    with repository(engine, household_id) as repo:
        policy = repo.policy()
        rows = repo.decisions()
    # **Every served day, newest first — not a window onto them.** This was capped at 40, which on a
    # 90-day household silently ended the list in late April: the operator scrolled to the bottom,
    # found March missing, and had no way to tell a truncated list from a household that simply had
    # no earlier decisions. A support tool that quietly omits history is worse than a long page.
    #
    # Unbounded is safe *here* because `repo.decisions()` already loads the whole window regardless
    # — the cap only ever hid rows that had been fetched anyway, so removing it costs no query time.
    # If served windows ever grow past a few hundred days, group this by month rather than
    # re-truncating it (see the scoping note in ticket 0070).
    rows = sorted(rows, key=lambda r: r["day"], reverse=True)
    decisions = [
        DecisionRow(
            day=_iso(r["day"]),
            action=str(r["action"]),
            amount=f"{r['amount']:,.2f}",
            target=r.get("target_debt_id"),
        )
        for r in rows
    ]
    return HouseholdDetail(
        status=status,
        buffer_floor=None if policy is None else f"{policy['buffer_floor']:,.2f}",
        max_sweep=None if policy is None else f"{policy['max_sweep']:,.2f}",
        min_days_between_sweeps=None if policy is None else policy["min_days_between_sweeps"],
        decisions=decisions,
    )


def _load_snapshot(engine: Engine, household_id: str, day: date):
    """The frozen snapshot behind one decision, or None if not stored."""
    with repository(engine, household_id) as repo:
        store = PostgresSnapshotStore(repo.conn)
        try:
            # Refs are opaque to callers and the Postgres store only answers to its own `pg:` refs
            # (`PostgresSnapshotStore.put` returns exactly this shape).
            return store.get(f"pg:{household_id}:{day.isoformat()}")
        except SnapshotStoreError:
            return None


def decision_trace(engine: Engine, household_id: str, day: date) -> DecisionTrace | None:
    """The exact snapshot behind one decision, replayed through the engine. None if not stored."""
    snapshot = _load_snapshot(engine, household_id, day)
    return trace(snapshot) if snapshot is not None else None


@dataclass(frozen=True)
class CardFact:
    card_id: str
    balance: str  # total owed = statement + unbilled
    statement: str
    apr: str
    behavior: str
    minimum: str


@dataclass(frozen=True)
class AccountFacts:
    """The raw inputs a decision was made on, straight off the snapshot — the operator's 'step 0'
    before the gate trace. Everything here is a stored field; the one thing that is *not* stored is
    the per-bucket payroll history behind `income variation`, so it is summarized, not itemized."""

    accounts: list[tuple[str, str, str]]  # (label, balance, connection·age)
    income_variation: str
    history_days: int
    cards: list[CardFact]


def account_facts(engine: Engine, household_id: str, day: date) -> AccountFacts | None:
    snapshot = _load_snapshot(engine, household_id, day)
    if snapshot is None:
        return None
    accounts = []
    for a in snapshot.accounts:
        label = a.kind.value + (" (funding)" if a.account_id == snapshot.funding_account_id else "")
        accounts.append((label, _f(a.balance), f"{a.connection.value} · {a.balance_age_days}d old"))
    cards = [
        CardFact(
            card_id=c.card_id,
            balance=_f(c.total_owed),
            statement=_f(c.statement_balance),
            apr=f"{c.apr:.2%} ({c.apr_source.value})" if c.apr is not None else "unknown",
            behavior=c.behavior.value,
            minimum=_f(c.minimum_payment),
        )
        for c in snapshot.portfolio.cards
    ]
    return AccountFacts(
        accounts=accounts,
        income_variation=f"{snapshot.income_variation:.2%}",
        history_days=snapshot.history_days,
        cards=cards,
    )


def _f(d: object) -> str:
    """A dollar figure, matching the trace's `$X,XXX.XX`."""
    return f"${d:,.2f}"


def _iso(d: object) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


def _dash(v: object) -> str:
    return "—" if v is None else str(v)


# --- the overrides: pause (one household) and halt (all of them) ---------------------
#
# Two controls, two mechanisms, and neither keeps a second copy of its own state. A **pause** is a
# forward window written into `policy_events.blackout_dates` — the same surface the customer's own
# pause uses, and the one `decide()` already refuses on (`decide.py`, `ReasonCode.BLACKOUT`). A
# **halt** is a row in the append-only `operator_actions` log; the halt *state* is simply the latest
# halt/resume row, never a mutable flag that could drift from the log that explains it.

# How far forward a pause blacks out. The engine checks one day at a time, so "paused" has to be a
# span of days, not a boolean; 90 is "paused for the foreseeable" without writing an unbounded set.
PAUSE_HORIZON_DAYS = 90


class OperatorActionError(RuntimeError):
    """An override could not be applied — e.g. pausing a household that has no policy to amend."""


class SystemHalted(RuntimeError):
    """The global halt is in effect. Raised by `assert_not_halted` — the hook the sweep-execution
    rung calls before moving money. Nothing moves money today (the transfer leg is shadow), so this
    is the wire left in place for when it does, not a guard already on a live path."""


@dataclass(frozen=True)
class OperatorAction:
    at: str
    actor: str
    action: str
    household_id: str | None
    detail: dict


def _insert_action(conn, actor: str, action: str, household_id: str | None, detail: dict) -> None:
    conn.execute(
        text(
            "INSERT INTO operator_actions (actor, action, household_id, detail)"
            " VALUES (:a, :act, :h, CAST(:d AS jsonb))"
        ),
        {"a": actor, "act": action, "h": household_id, "d": json.dumps(detail)},
    )


def _rewrite_blackout(repo, policy: dict, dates: list[date]) -> None:
    """Re-append the household's policy with a new blackout set, guardrails otherwise unchanged.

    `changed_by` is NULL: the operator is a god-mode credential, not a `users` row. `loosened` is
    False — a pause tightens, it never weakens a guardrail."""
    repo.set_policy(
        buffer_floor=policy["buffer_floor"],
        max_sweep=policy["max_sweep"],
        max_weekly_sweep=policy["max_weekly_sweep"],
        min_days_between_sweeps=policy["min_days_between_sweeps"],
        blackout_dates=[d.isoformat() for d in dates],
        changed_by=None,
        loosened=False,
    )


def pause_household(
    engine: Engine,
    actor: str,
    household_id: str,
    *,
    today: date | None = None,
    horizon_days: int = PAUSE_HORIZON_DAYS,
) -> None:
    """Pause sweeps for one household: blackout today forward, and log it. Atomic — the policy write
    and the audit row commit together, so a pause never lands unrecorded."""
    ref = today or date.today()
    with repository(engine, household_id) as repo:
        policy = repo.policy()
        if policy is None:
            raise OperatorActionError(f"{household_id} has no policy to pause")
        window = {ref + timedelta(days=i) for i in range(horizon_days)}
        merged = sorted(set(policy["blackout_dates"]) | window)
        _rewrite_blackout(repo, policy, merged)
        through = (ref + timedelta(days=horizon_days - 1)).isoformat()
        _insert_action(
            repo.conn, actor, "pause", household_id, {"from": ref.isoformat(), "through": through}
        )


def unpause_household(
    engine: Engine, actor: str, household_id: str, *, today: date | None = None
) -> None:
    """Lift an operator pause: drop every blacked-out day from today forward, and log it. Dates in
    the past are left alone — they are history, not a pause anyone can still act on."""
    ref = today or date.today()
    with repository(engine, household_id) as repo:
        policy = repo.policy()
        if policy is None:
            raise OperatorActionError(f"{household_id} has no policy to unpause")
        remaining = sorted(d for d in policy["blackout_dates"] if d < ref)
        _rewrite_blackout(repo, policy, remaining)
        _insert_action(repo.conn, actor, "unpause", household_id, {"cleared_from": ref.isoformat()})


def set_global_halt(engine: Engine, actor: str, halted: bool) -> None:
    """Record a global halt or resume. State is the latest such row (see `is_globally_halted`)."""
    with engine.begin() as conn:
        _insert_action(conn, actor, "halt" if halted else "resume", None, {})


def is_globally_halted(engine: Engine) -> bool:
    """True iff the most recent global action was a halt. Derived from the log, never stored."""
    with engine.connect() as conn:
        latest = conn.execute(
            text(
                "SELECT action FROM operator_actions WHERE action IN ('halt', 'resume')"
                " ORDER BY seq DESC LIMIT 1"
            )
        ).scalar()
    return latest == "halt"


def assert_not_halted(engine: Engine) -> None:
    """Raise if a global halt is in effect. The hook the sweep-execution rung will call before it
    moves money; deliberately *not* wired into any live path yet (nothing moves money today)."""
    if is_globally_halted(engine):
        raise SystemHalted("a global halt is in effect — no money movement permitted")


def recent_actions(engine: Engine, limit: int = 25) -> list[OperatorAction]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT at, actor, action, household_id, detail FROM operator_actions"
                    " ORDER BY seq DESC LIMIT :n"
                ),
                {"n": limit},
            )
            .mappings()
            .all()
        )
    return [
        OperatorAction(
            at=str(r["at"]),
            actor=r["actor"],
            action=r["action"],
            household_id=r["household_id"],
            detail=r["detail"],
        )
        for r in rows
    ]


# --- rendering -----------------------------------------------------------------------


def _theme_picker(theme: str | None, path: str) -> str:
    """Auto / Light / Dark, as three submit buttons in one form.

    Three states rather than a two-way toggle because the server cannot see the operating
    system's preference — a toggle would have to guess what it is currently flipping away from,
    and guess wrong half the time. Plain form posts, no JavaScript.
    """
    choices = (
        ("system", "Auto", theme is None),
        ("light", "Light", theme == "light"),
        ("dark", "Dark", theme == "dark"),
    )
    buttons = "".join(
        f'<button class="tbtn{" on" if on else ""}" name="to" value="{value}"'
        f"{' aria-current=true' if on else ''}>{label}</button>"
        for value, label, on in choices
    )
    return (
        '<form method="post" action="/theme" class="theme" aria-label="Color theme">'
        f'<input type="hidden" name="next" value="{html.escape(_safe_next(path), quote=True)}">'
        f"{buttons}</form>"
    )


def _page(
    title: str,
    body: str,
    *,
    user: str | None = None,
    halted: bool = False,
    theme: str | None = None,
    path: str = "/",
) -> str:
    chrome = ""
    if user is not None:
        strip = '<div class="haltstrip">Money movement is halted</div>' if halted else ""
        chrome = (
            '<div class="topbar"><a class="brand" href="/">CFO&nbsp;·&nbsp;Operator</a>'
            f'<span class="who">{html.escape(user)}'
            f"{_theme_picker(theme, path)}"
            '<form method="post" action="/logout" class="inline">'
            '<button class="linkbtn" type="submit">sign out</button></form></span></div>'
            f"{strip}"
        )
    # Re-validated here rather than trusted from the caller: this is the single point where the
    # value becomes markup, so it is the one place the allowlist has to hold.
    attr = f' data-theme="{theme}"' if theme in THEMES else ""
    return (
        f"<!doctype html><html lang=en{attr}><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        f"{chrome}<main class=wrap>{body}</main></body></html>"
    )


def _login_page(error: str | None = None, theme: str | None = None) -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = (
        '<section class="login">'
        '<span class="eyebrow">Operator console</span>'
        "<h1>Sign in</h1>"
        '<p class="sub">God-mode access. Placeholder auth — real per-operator sign-in is still to '
        "come.</p>"
        f"{err}"
        '<form method="post" action="/login" class="stack">'
        '<label>Operator<input name="user" autocomplete="username" autofocus></label>'
        '<label>Password<input name="password" type="password" '
        'autocomplete="current-password"></label>'
        '<button class="primary" type="submit">Enter</button>'
        "</form></section>"
    )
    return _page("Operator · sign in", body, theme=theme)


def _badge(status: HouseholdStatus) -> str:
    if status.paused:
        return '<span class="pill paused">paused</span>'
    return '<span class="pill active">active</span>'


def _halt_control(halted: bool) -> str:
    if halted:
        return (
            '<div class="haltcard on"><div><span class="hstatus">Money movement halted</span>'
            "<p>A global halt is in effect. The sweep-execution rung refuses to move money while "
            "this stands.</p></div>"
            '<form method="post" action="/resume"><button class="btn resume">Resume</button></form>'
            "</div>"
        )
    return (
        '<div class="haltcard off"><div><span class="hstatus">Money movement live</span>'
        "<p>The global kill switch. Halting stops all money movement at the execution rung; it is "
        "recorded to the audit log below.</p></div>"
        '<form method="post" action="/halt"><button class="btn halt">Halt all</button></form>'
        "</div>"
    )


def _audit_html(actions: list[OperatorAction]) -> str:
    if not actions:
        return '<p class="muted">No operator actions recorded yet.</p>'
    rows = ""
    for a in actions:
        where = html.escape(a.household_id) if a.household_id else "all households"
        rows += (
            f'<div class="arow"><span class="aat">{html.escape(a.at[:19])}</span>'
            f'<span class="aact {html.escape(a.action)}">{html.escape(a.action)}</span>'
            f'<span class="awho">{html.escape(where)}</span>'
            f'<span class="aactor">{html.escape(a.actor)}</span></div>'
        )
    return f'<div class="alist">{rows}</div>'


def _dashboard(
    rows: list[HouseholdStatus],
    user: str,
    halted: bool,
    actions: list[OperatorAction],
    theme: str | None = None,
) -> str:
    items = ""
    for s in rows:
        last = (
            f'<span class="last">{html.escape(s.last_action)} · {html.escape(s.last_day)}</span>'
            if s.last_action
            else '<span class="last muted">no decisions</span>'
        )
        demo = '<span class="tag">demo</span>' if s.is_demo else ""
        items += (
            f'<a class="hrow" href="/household/{html.escape(s.id)}">'
            f'<span class="hmain"><span class="hid">{html.escape(s.id)}</span>'
            f'<span class="hlabel">{html.escape(s.label)}{demo}</span></span>'
            f'<span class="hstate">{_badge(s)}{last}</span></a>'
        )
    body = (
        '<div class="head"><div><span class="eyebrow">Households</span>'
        f"<h1>{len(rows)} household{'s' if len(rows) != 1 else ''}</h1></div></div>"
        f"{_halt_control(halted)}"
        f'<div class="hlist">{items}</div>'
        "<h2>Recent operator actions</h2>"
        f"{_audit_html(actions)}"
    )
    return _page("Operator · households", body, user=user, halted=halted, theme=theme, path="/")


def _pill_class(step: GateStep) -> tuple[str, str]:
    if step.stage == "Decision":
        return "swept", "s-sweep"
    if step.result == BLOCK:
        return ("held", "s-hold") if step.stage == "Cadence" else ("blocked", "s-hold")
    if step.result == PASS:
        return "cleared", "s-pass"
    return "reading", "s-info"


def _trace_html(t: DecisionTrace) -> str:
    steps = ""
    for step in t.steps:
        label, cls = _pill_class(step)
        term = " terminal" if step.terminal else ""
        if step.stage == "Surplus":
            # A vertical ledger: the projected low, then the buffer and each card's reserve
            # subtracted one per row, then the available result. Scales to however many cards.
            rows = ""
            for k, v in step.inputs.items():
                rcls = "total" if k.startswith("=") else ("sub" if k.startswith("−") else "base")
                rows += (
                    f'<div class="lrow {rcls}"><span>{html.escape(k)}</span>'
                    f"<span>{html.escape(v)}</span></div>"
                )
            inputs = f'<div class="ledger">{rows}</div>'
        else:
            inputs = (
                '<dl class="kv">'
                + "".join(
                    f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>"
                    for k, v in step.inputs.items()
                )
                + "</dl>"
            )
        threshold = (
            (
                '<div class="thr"><span>threshold</span>'
                f"<code>{html.escape(step.threshold)}</code></div>"
            )
            if step.threshold
            else ""
        )
        steps += (
            f'<li class="step {cls}{term}">'
            f'<div class="sh"><span class="ord">{step.order}</span>'
            f'<span class="st"><em>{html.escape(step.stage)}</em>{html.escape(step.name)}</span>'
            f'<span class="spill {cls}">{label}</span></div>'
            f'<p class="q">{html.escape(step.question)}</p>{inputs}{threshold}'
            f'<p class="det">{html.escape(step.detail)}</p></li>'
        )
    return f'<ol class="steps">{steps}</ol>'


def _facts_html(facts: AccountFacts) -> str:
    """The unofficial 'step 0' — the raw account inputs the decision was made on."""
    accounts = "".join(
        f"<div><dt>{html.escape(label)}</dt>"
        f'<dd>{html.escape(bal)}<span class="fnote">{html.escape(note)}</span></dd></div>'
        for label, bal, note in facts.accounts
    )
    card_rows = "".join(
        f"<tr><td>{html.escape(c.card_id)}</td><td>{html.escape(c.balance)}</td>"
        f"<td>{html.escape(c.apr)}</td><td>{html.escape(c.behavior)}</td>"
        f"<td>{html.escape(c.statement)}</td><td>{html.escape(c.minimum)}</td></tr>"
        for c in facts.cards
    )
    return (
        '<div class="facts">'
        '<div class="fh"><span class="ord0">0</span>'
        '<span class="st"><em>Account facts</em>What the engine is looking at</span></div>'
        '<div class="fgrid">'
        '<div class="fcol"><h4>Accounts</h4>'
        f'<dl class="kv">{accounts}</dl></div>'
        '<div class="fcol"><h4>Income</h4>'
        '<dl class="kv">'
        "<div><dt>income variation (per paycheck)</dt>"
        f"<dd>{html.escape(facts.income_variation)}</dd></div>"
        f"<div><dt>transaction history</dt><dd>{facts.history_days} days</dd></div>"
        "</dl>"
        '<p class="fnote block">Variation is the coefficient of variation of '
        "<b>paycheck amounts</b> over the trailing ~84 days — timing-agnostic, so it does not "
        "alias against pay cadence "
        "(biweekly / semimonthly / monthly all read the same). The individual paycheck figures are "
        "not in the stored snapshot.</p></div>"
        "</div>"
        f"<h4>Cards ({len(facts.cards)})</h4>"
        '<table class="cards"><thead><tr><th>card</th><th>balance</th><th>APR</th>'
        "<th>behaviour</th><th>statement</th><th>min</th></tr></thead>"
        f"<tbody>{card_rows}</tbody></table>"
        "</div>"
    )


def _pause_control(s: HouseholdStatus) -> str:
    if s.paused:
        return (
            '<form method="post" action="/household/'
            f'{html.escape(s.id)}/unpause" class="pausebox paused">'
            "<div><span>Sweeps paused</span>"
            "<p>This household will refuse to sweep while paused.</p>"
            '</div><button class="btn resume">Resume sweeps</button></form>'
        )
    return (
        '<form method="post" action="/household/'
        f'{html.escape(s.id)}/pause" class="pausebox">'
        "<div><span>Sweeps active</span><p>Pause blacks out sweeps for this household going "
        'forward.</p></div><button class="btn halt">Pause sweeps</button></form>'
    )


def _household_page(
    detail: HouseholdDetail,
    user: str,
    trace_view: DecisionTrace | None,
    open_day: str | None,
    halted: bool = False,
    facts: AccountFacts | None = None,
    theme: str | None = None,
) -> str:
    s = detail.status
    # A pause writes a contiguous forward window; a full date dump would flood the page, so show the
    # span, not every day. Kept exact (min → max, count) so nothing is rounded away.
    if not s.blackout_dates:
        black = "none"
    elif len(s.blackout_dates) == 1:
        black = s.blackout_dates[0]
    else:
        black = f"{min(s.blackout_dates)} → {max(s.blackout_dates)} ({len(s.blackout_dates)} days)"
    decisions = ""
    for d in detail.decisions:
        active = " open" if d.day == open_day else ""
        amt = f"${d.amount}" if d.action == "sweep" else "—"
        href = f"/household/{html.escape(s.id)}/decision/{html.escape(d.day)}"
        decisions += (
            f'<a class="drow{active}" href="{href}">'
            f'<span class="dday">{html.escape(d.day)}</span>'
            f'<span class="dact {html.escape(d.action)}">{html.escape(d.action)}</span>'
            f'<span class="damt">{amt}</span></a>'
        )
    empty_panel = (
        '<div class="tracepanel empty"><p class="muted">Select a decision to see the exact '
        "snapshot and every gate behind it.</p></div>"
    )
    tracepanel = ""
    if open_day is not None:
        if trace_view is None:
            tracepanel = (
                '<div class="tracepanel empty"><h2>No snapshot stored</h2>'
                f"<p>There is no frozen snapshot for {html.escape(open_day)}, so the exact inputs "
                "behind this decision cannot be shown. Older seeded rows may predate the snapshot "
                "store.</p></div>"
            )
        else:
            amt = f" ${trace_view.amount:,.2f}" if trace_view.action == "sweep" else ""
            facts_block = _facts_html(facts) if facts is not None else ""
            tracepanel = (
                '<div class="tracepanel"><div class="tphead">'
                f"<h2>Snapshot behind {html.escape(open_day)}</h2>"
                f'<span class="tpout {trace_view.action}">'
                f"{html.escape(trace_view.action)}{amt}</span></div>"
                f"{facts_block}{_trace_html(trace_view)}</div>"
            )
    body = (
        f'<a class="back" href="/">← all households</a>'
        f'<div class="head"><div><span class="eyebrow">Household</span>'
        f"<h1>{html.escape(s.id)}</h1>"
        f'<p class="sub">{html.escape(s.label)} · {_badge(s)}</p></div></div>'
        f"{_pause_control(s)}"
        '<div class="cols">'
        '<section class="col">'
        "<h2>Policy</h2>"
        '<dl class="policy">'
        f"<div><dt>buffer floor</dt><dd>${html.escape(detail.buffer_floor or '—')}</dd></div>"
        f"<div><dt>max sweep</dt><dd>${html.escape(detail.max_sweep or '—')}</dd></div>"
        f"<div><dt>min days between sweeps</dt><dd>{_dash(detail.min_days_between_sweeps)}"
        "</dd></div>"
        f"<div><dt>paused dates</dt><dd>{html.escape(black)}</dd></div>"
        "</dl>"
        "<h2>Decisions</h2>"
        f'<div class="dlist">{decisions or "<p class=muted>none</p>"}</div>'
        "</section>"
        f'<section class="col wide">{tracepanel or empty_panel}</section>'
        "</div>"
    )
    # So that picking a theme keeps you on the decision you were reading, not just the household.
    here = f"/household/{s.id}" + (f"/decision/{open_day}" if open_day else "")
    return _page(f"Operator · {s.id}", body, user=user, halted=halted, theme=theme, path=here)


# --- app -----------------------------------------------------------------------------


def create_operator_app(engine: Engine | None = None) -> FastAPI:
    """The console as a standalone ASGI app. Deliberately its own app, not a router on the
    customer API — different port, different audience, different auth."""
    _operator_password()  # fail loud at startup if unconfigured
    app = FastAPI(title="CFO Operator Console", docs_url=None, redoc_url=None)
    app.state.engine = engine or make_engine()

    def require(request: Request) -> str | None:
        return _session_user(request)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> HTMLResponse:
        if require(request):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_login_page(theme=_session_theme(request)))

    @app.post("/login")
    async def login(request: Request) -> object:
        # Parse the urlencoded form body directly rather than via `fastapi.Form`, which would pull
        # in `python-multipart` as a dependency for one login field.
        form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
        user = form.get("user", [""])[0]
        password = form.get("password", [""])[0]
        if not _check_login(user, password):
            return HTMLResponse(
                _login_page("Wrong operator or password.", _session_theme(request)),
                status_code=401,
            )
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE,
            _issue_session(user),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return resp

    @app.post("/logout")
    async def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/theme")
    async def theme(request: Request) -> object:
        """Pin Light or Dark, or clear the pin back to Auto. Behind the gate like everything
        else, and it never touches `operator_actions` — a color preference is not an override."""
        if not require(request):
            return RedirectResponse("/login", status_code=303)
        form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
        want = form.get("to", [""])[0]
        if want not in (*THEMES, "system"):
            return HTMLResponse("Unknown theme.", status_code=400)
        resp = RedirectResponse(_safe_next(form.get("next", ["/"])[0]), status_code=303)
        if want == "system":
            resp.delete_cookie(THEME_COOKIE)
        else:
            # Not `httponly`: this one is a display preference, and leaving it readable means a
            # future client-side enhancement needs no second source of truth. Nothing is
            # authorised by it — `_page` allowlists the value before it becomes markup.
            resp.set_cookie(THEME_COOKIE, want, max_age=THEME_TTL_SECONDS, samesite="lax")
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        eng = app.state.engine
        return HTMLResponse(
            _dashboard(
                list_status(eng),
                user,
                is_globally_halted(eng),
                recent_actions(eng),
                _session_theme(request),
            )
        )

    @app.get("/household/{household_id}", response_class=HTMLResponse)
    async def household(request: Request, household_id: str) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        eng = app.state.engine
        detail = household_detail(eng, household_id)
        if detail is None:
            return HTMLResponse(
                _page(
                    "Not found",
                    "<h1>No such household</h1>",
                    user=user,
                    theme=_session_theme(request),
                ),
                status_code=404,
            )
        return HTMLResponse(
            _household_page(
                detail, user, None, None, is_globally_halted(eng), theme=_session_theme(request)
            )
        )

    @app.get("/household/{household_id}/decision/{day}", response_class=HTMLResponse)
    async def decision(request: Request, household_id: str, day: str) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        eng = app.state.engine
        detail = household_detail(eng, household_id)
        if detail is None:
            return HTMLResponse(
                _page(
                    "Not found",
                    "<h1>No such household</h1>",
                    user=user,
                    theme=_session_theme(request),
                ),
                status_code=404,
            )
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            return HTMLResponse(
                _page("Bad date", "<h1>Bad date</h1>", user=user, theme=_session_theme(request)),
                status_code=400,
            )
        t = decision_trace(eng, household_id, parsed)
        facts = account_facts(eng, household_id, parsed)
        return HTMLResponse(
            _household_page(
                detail, user, t, day, is_globally_halted(eng), facts, theme=_session_theme(request)
            )
        )

    # --- overrides (writes) ---------------------------------------------------------
    # No CSRF token: this is a single-operator, same-origin local tool behind a god-mode password.
    # When real per-operator auth lands, so does a CSRF defence — noted in the module docstring.

    @app.post("/household/{household_id}/pause")
    async def pause(request: Request, household_id: str) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        pause_household(app.state.engine, user, household_id)
        return RedirectResponse(f"/household/{household_id}", status_code=303)

    @app.post("/household/{household_id}/unpause")
    async def unpause(request: Request, household_id: str) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        unpause_household(app.state.engine, user, household_id)
        return RedirectResponse(f"/household/{household_id}", status_code=303)

    @app.post("/halt")
    async def halt(request: Request) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        set_global_halt(app.state.engine, user, True)
        return RedirectResponse("/", status_code=303)

    @app.post("/resume")
    async def resume(request: Request) -> object:
        user = require(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        set_global_halt(app.state.engine, user, False)
        return RedirectResponse("/", status_code=303)

    return app


# The console stylesheet lives beside this module as plain CSS rather than a Python string —
# a long CSS blob is not Python and should not be linted as if it were.
CSS = (Path(__file__).with_name("operator.css")).read_text(encoding="utf-8")


def main() -> None:
    import uvicorn

    port = int(os.environ.get("OPERATOR_PORT", "8787"))
    _operator_password()
    print(f"operator console → http://127.0.0.1:{port}  (user: {_operator_user()})")
    uvicorn.run(create_operator_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
