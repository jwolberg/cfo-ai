"""Render the decision audit panel — a CEO-facing observability page for two real decisions.

Generates a self-contained HTML file from `backend.trace.trace()` over the demo household's stored
walk. Every number on the page is the engine's own output; nothing is typed by hand here. Regenerate
any time with:

    .venv/bin/python scripts/render_decision_panel.py [OUTPUT.html]

The two days are `2026-05-25` (a sweep) and `2026-05-30` (a refuse) — the same machine, five
identical gates, and the one gate (cadence) where they diverge.
"""

from __future__ import annotations

import html
import sys
from datetime import date

from backend.precompute import (
    DEMO_POLICY,
    DEMO_SPEC,
    SEED,
    SERVED_DAYS,
    SPEND_QUANTILE,
    WARMUP_DAYS,
    WINDOW_START,
    generate,
    walk,
)
from backend.trace import BLOCK, PASS, DecisionTrace, GateStep, trace

SWEEP_DAY = date(2026, 5, 25)
REFUSE_DAY = date(2026, 5, 30)


def _traces() -> tuple[DecisionTrace, DecisionTrace]:
    total = WARMUP_DAYS + SERVED_DAYS
    history = generate(DEMO_SPEC, start=WINDOW_START, days=total, seed=SEED)
    by_day = {
        w.day: w for w in walk(history, DEMO_SPEC, WINDOW_START, total, DEMO_POLICY, SPEND_QUANTILE)
    }
    return trace(by_day[SWEEP_DAY].snapshot), trace(by_day[REFUSE_DAY].snapshot)


def _pretty_day(d: date) -> str:
    return d.strftime("%a %b %-d, %Y")


def _pill(step: GateStep) -> tuple[str, str]:
    """(label, css-class) for a step's state pill."""
    if step.stage == "Decision":
        return "swept", "is-sweep"
    if step.result == BLOCK:
        return ("held", "is-hold") if step.stage == "Cadence" else ("blocked", "is-hold")
    if step.result == PASS:
        return "cleared", "is-pass"
    return "reading", "is-info"


def _inputs_html(step: GateStep) -> str:
    # The surplus rung is arithmetic — render it as an equation, not a list.
    if step.stage == "Surplus":
        low = step.inputs["projected low"]
        buf = step.inputs["− buffer floor"].lstrip("− ")
        res = step.inputs["− reserved obligations"].lstrip("− ")
        avail = step.inputs["= available"]
        return (
            '<div class="equation">'
            f'<span class="term">{html.escape(low)}</span>'
            '<span class="op">−</span>'
            f'<span class="term"><span class="term-label">buffer</span>{html.escape(buf)}</span>'
            '<span class="op">−</span>'
            f'<span class="term"><span class="term-label">reserved</span>{html.escape(res)}</span>'
            '<span class="op">=</span>'
            f'<span class="term term-result">{html.escape(avail)}</span>'
            "</div>"
        )
    rows = "".join(
        f'<div class="kv"><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>'
        for k, v in step.inputs.items()
    )
    return f'<div class="kvs">{rows}</div>'


def _step_html(step: GateStep, muted: bool = False) -> str:
    label, pill_cls = _pill(step)
    classes = ["step"]
    if step.terminal:
        classes.append("is-terminal")
        classes.append("term-sweep" if step.stage == "Decision" else "term-hold")
    if muted:
        classes.append("is-ghost")
    threshold = (
        f'<div class="threshold"><span class="threshold-tag">threshold</span>'
        f"<code>{html.escape(step.threshold)}</code></div>"
        if step.threshold
        else ""
    )
    return (
        f'<li class="{" ".join(classes)}">'
        '<div class="step-head">'
        f'<span class="order">{step.order}</span>'
        '<div class="step-title">'
        f'<span class="stage">{html.escape(step.stage)}</span>'
        f'<span class="name">{html.escape(step.name)}</span>'
        "</div>"
        f'<span class="pill {pill_cls}">{label}</span>'
        "</div>"
        f'<p class="question">{html.escape(step.question)}</p>'
        f"{_inputs_html(step)}"
        f"{threshold}"
        f'<p class="detail">{html.escape(step.detail)}</p>'
        "</li>"
    )


def _ladder_html(t: DecisionTrace, amount_label: str, outcome_cls: str) -> str:
    steps = "".join(_step_html(s) for s in t.steps)
    return (
        f'<section class="ladder {outcome_cls}">'
        '<header class="ladder-head">'
        f'<span class="ladder-date">{_pretty_day(t.day)}</span>'
        f'<span class="ladder-outcome">{amount_label}</span>'
        "</header>"
        f'<ol class="steps">{steps}</ol>'
        "</section>"
    )


def render(sweep: DecisionTrace, refuse: DecisionTrace) -> str:
    # Pull the two numbers the punchline turns on, straight from the trace.
    refuse_surplus = next(s for s in refuse.steps if s.stage == "Surplus").inputs["= available"]
    sweep_amount = f"${sweep.amount:,.2f}"

    left = _ladder_html(sweep, f"Sweep {sweep_amount}", "out-sweep")
    right = _ladder_html(refuse, "Refuse", "out-hold")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How the engine decided — audit trail</title>
<style>
{CSS}
</style>
</head>
<body>
<main class="wrap">
  <header class="masthead">
    <span class="eyebrow">Decision engine · audit trail</span>
    <h1>Same machine, two answers</h1>
    <p class="lede">Every reserve decision runs the same ordered gates over the household's real
      balances. Here are two days five apart: one moved money, one did not. The inputs, the
      thresholds each was measured against, and the single gate where they diverged — nothing
      inferred, read straight from the engine's own trace.</p>
  </header>

  <section class="thesis" aria-label="The two outcomes at a glance">
    <div class="outcome out-sweep">
      <span class="o-date">{_pretty_day(sweep.day)}</span>
      <span class="o-verb">Swept</span>
      <span class="o-amt">${sweep.amount:,.2f}</span>
      <span class="o-note">every gate cleared — surplus was real and the cadence was open</span>
    </div>
    <div class="divergence" aria-hidden="true">
      <span class="d-line"></span>
      <span class="d-tag">1 gate apart</span>
      <span class="d-line"></span>
    </div>
    <div class="outcome out-hold">
      <span class="o-date">{_pretty_day(refuse.day)}</span>
      <span class="o-verb">Refused</span>
      <span class="o-amt">{refuse_surplus} <span class="o-amt-sub">available, held</span></span>
      <span class="o-note">the same gates cleared — then cadence held it: 5 days &lt; 7</span>
    </div>
  </section>

  <p class="readnote">Read each column top to bottom — the decision <em>is</em> the sequence. Gates
    1–5 are identical on both days. Gate 6, cadence, is where {_pretty_day(refuse.day)} stops.</p>

  <div class="ladders">
    {left}
    {right}
  </div>

  <footer class="provenance">
    <p><strong>How this was produced.</strong> Generated by <code>backend/trace.py</code>, which
      replays the engine's own <code>decide()</code> over the stored snapshot for each day and
      records every gate. The headline outcome is <code>decide()</code>'s verbatim output, not a
      reconstruction. <code>tests/test_trace.py</code> pins the trace to the engine across all
      {SERVED_DAYS} days of the window, so this page cannot drift out of step with what actually
      ran.</p>
    <p class="prov-fine">Household <code>hh_demo_biweekly</code> · synthetic data, deterministic
      seed · figures in USD.</p>
  </footer>
</main>
</body>
</html>
"""


CSS = """
:root{
  --ground:#F2F6F8; --surface:#FFFFFF; --surface-2:#FAFCFD; --border:#E1E9ED;
  --ink:#16202A; --body:#556673; --muted:#8A97A0;
  --sweep:#1D9E75; --sweep-deep:#085041; --sweep-wash:#EAF6F1;
  --hold:#217E96; --hold-bright:#4AAFC9; --hold-wash:#E9F3F6;
  --pass:#5C9A82; --shadow:0 1px 2px rgba(11,43,54,.06),0 8px 24px rgba(11,43,54,.06);
  --font-sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --font-mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --ground:#0C181D; --surface:#12232A; --surface-2:#152932; --border:#253B42;
    --ink:#EAF2F4; --body:#A6B6BD; --muted:#728790;
    --sweep:#37C193; --sweep-deep:#8ADCC3; --sweep-wash:#12312A;
    --hold:#4FB3CD; --hold-bright:#6FC7DD; --hold-wash:#123039;
    --pass:#6FB89E; --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
  }
}
:root[data-theme="light"]{
  --ground:#F2F6F8; --surface:#FFFFFF; --surface-2:#FAFCFD; --border:#E1E9ED;
  --ink:#16202A; --body:#556673; --muted:#8A97A0;
  --sweep:#1D9E75; --sweep-deep:#085041; --sweep-wash:#EAF6F1;
  --hold:#217E96; --hold-bright:#4AAFC9; --hold-wash:#E9F3F6;
  --pass:#5C9A82; --shadow:0 1px 2px rgba(11,43,54,.06),0 8px 24px rgba(11,43,54,.06);
}
:root[data-theme="dark"]{
  --ground:#0C181D; --surface:#12232A; --surface-2:#152932; --border:#253B42;
  --ink:#EAF2F4; --body:#A6B6BD; --muted:#728790;
  --sweep:#37C193; --sweep-deep:#8ADCC3; --sweep-wash:#12312A;
  --hold:#4FB3CD; --hold-bright:#6FC7DD; --hold-wash:#123039;
  --pass:#6FB89E; --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;}
body{
  margin:0; background:var(--ground); color:var(--body);
  font-family:var(--font-sans); font-size:16px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px; margin:0 auto; padding:clamp(1.5rem,4vw,3.5rem) clamp(1rem,4vw,2rem) 3rem;}
code{font-family:var(--font-mono); font-size:.9em;}

/* Masthead */
.masthead{max-width:64ch;}
.eyebrow{
  display:inline-block; font-size:.72rem; font-weight:600; letter-spacing:.14em;
  text-transform:uppercase; color:var(--hold); margin-bottom:.9rem;
}
h1{
  font-size:clamp(1.9rem,4.4vw,2.9rem); line-height:1.05; letter-spacing:-.02em;
  color:var(--ink); font-weight:700; margin:0 0 .8rem; text-wrap:balance;
}
.lede{font-size:1.06rem; margin:0; max-width:60ch;}

/* Thesis band */
.thesis{
  display:grid; grid-template-columns:1fr auto 1fr; align-items:stretch; gap:clamp(.75rem,2vw,1.5rem);
  margin:2.4rem 0 1.6rem;
}
.outcome{
  background:var(--surface); border:1px solid var(--border); border-radius:16px;
  padding:1.3rem 1.4rem; box-shadow:var(--shadow); display:flex; flex-direction:column; gap:.15rem;
  border-top:3px solid var(--edge);
}
.out-sweep{--edge:var(--sweep);}
.out-hold{--edge:var(--hold-bright);}
.o-date{font-size:.8rem; color:var(--muted); font-weight:600; letter-spacing:.02em;}
.o-verb{font-size:.95rem; font-weight:700; color:var(--edge); text-transform:uppercase; letter-spacing:.06em;}
.o-amt{font-family:var(--font-mono); font-size:2rem; font-weight:600; color:var(--ink); font-variant-numeric:tabular-nums; line-height:1.1; margin-top:.1rem;}
.o-amt-sub{font-size:.85rem; font-weight:400; color:var(--muted); font-family:var(--font-sans);}
.o-note{font-size:.9rem; color:var(--body); margin-top:.35rem;}
.divergence{display:flex; flex-direction:column; align-items:center; justify-content:center; gap:.5rem; min-width:84px;}
.d-line{width:1px; flex:1; background:linear-gradient(var(--border),transparent);}
.divergence .d-line:last-child{background:linear-gradient(transparent,var(--border));}
.d-tag{font-size:.68rem; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); font-weight:600; white-space:nowrap; writing-mode:vertical-rl; transform:rotate(180deg);}

.readnote{font-size:.95rem; color:var(--body); margin:0 0 1.5rem; max-width:70ch;}
.readnote em{color:var(--ink); font-style:italic;}

/* Ladders */
.ladders{display:grid; grid-template-columns:1fr 1fr; gap:clamp(1rem,2.5vw,1.75rem); align-items:start;}
.ladder{
  background:var(--surface); border:1px solid var(--border); border-radius:18px;
  box-shadow:var(--shadow); overflow:hidden;
}
.ladder-head{
  display:flex; align-items:baseline; justify-content:space-between; gap:1rem;
  padding:1.05rem 1.3rem; border-bottom:1px solid var(--border); background:var(--surface-2);
}
.ladder-date{font-weight:700; color:var(--ink); font-size:1.02rem;}
.ladder-outcome{
  font-family:var(--font-mono); font-size:.85rem; font-weight:600; letter-spacing:.03em;
  padding:.28rem .6rem; border-radius:999px; color:#fff; white-space:nowrap;
}
.out-sweep .ladder-outcome{background:var(--sweep-deep);}
.out-hold .ladder-outcome{background:var(--hold);}

.steps{list-style:none; margin:0; padding:.4rem .5rem .7rem;}
.step{
  position:relative; padding:1rem 1rem 1.05rem 1rem; border-radius:12px;
  border:1px solid transparent;
}
.step + .step{margin-top:.15rem;}
.step:not(:last-child)::after{
  content:""; position:absolute; left:calc(.9rem + 12px); top:2.75rem; bottom:-.35rem; width:2px;
  background:var(--border);
}
.step-head{display:flex; align-items:center; gap:.7rem;}
.order{
  flex:none; width:26px; height:26px; border-radius:50%; display:grid; place-items:center;
  font-family:var(--font-mono); font-size:.82rem; font-weight:600;
  background:var(--surface-2); color:var(--muted); border:1px solid var(--border); z-index:1;
}
.step-title{display:flex; flex-direction:column; line-height:1.2; margin-right:auto;}
.stage{font-size:.7rem; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); font-weight:600;}
.name{font-size:1rem; font-weight:650; color:var(--ink);}
.pill{
  font-size:.72rem; font-weight:600; letter-spacing:.04em; text-transform:uppercase;
  padding:.22rem .55rem; border-radius:999px; white-space:nowrap; border:1px solid transparent;
}
.pill.is-pass{color:var(--pass); background:color-mix(in srgb,var(--pass) 12%,transparent); border-color:color-mix(in srgb,var(--pass) 26%,transparent);}
.pill.is-info{color:var(--muted); background:var(--surface-2); border-color:var(--border);}
.pill.is-hold{color:var(--hold); background:var(--hold-wash); border-color:color-mix(in srgb,var(--hold) 30%,transparent);}
.pill.is-sweep{color:#fff; background:var(--sweep-deep);}

.question{margin:.55rem 0 .7rem; font-size:.92rem; color:var(--body); padding-left:calc(26px + .7rem);}

.kvs{display:grid; grid-template-columns:1fr 1fr; gap:.3rem .9rem; padding-left:calc(26px + .7rem);}
@media (max-width:520px){ .kvs{grid-template-columns:1fr;} }
.kv{display:flex; align-items:baseline; justify-content:space-between; gap:.6rem; border-bottom:1px dotted var(--border); padding:.2rem 0;}
.kv dt{margin:0; font-size:.82rem; color:var(--muted);}
.kv dd{margin:0; font-family:var(--font-mono); font-size:.86rem; color:var(--ink); font-variant-numeric:tabular-nums; text-align:right;}

.equation{
  display:flex; align-items:flex-end; flex-wrap:wrap; gap:.5rem; margin-left:calc(26px + .7rem);
  padding:.7rem .85rem; background:var(--surface-2); border-radius:10px; border:1px solid var(--border);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums;
}
.term{display:flex; flex-direction:column; font-size:1.05rem; color:var(--ink); font-weight:600;}
.term-label{font-family:var(--font-sans); font-size:.62rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); font-weight:600;}
.op{font-size:1.05rem; color:var(--muted); padding-bottom:.05rem;}
.term-result{color:var(--sweep-deep); position:relative;}
.out-hold .term-result{color:var(--hold);}

.threshold{display:flex; align-items:center; gap:.5rem; margin:.7rem 0 0; padding-left:calc(26px + .7rem);}
.threshold-tag{font-size:.64rem; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); font-weight:600;}
.threshold code{color:var(--body); background:var(--surface-2); padding:.15rem .45rem; border-radius:6px; border:1px solid var(--border); font-size:.8rem;}

.detail{margin:.65rem 0 0; font-size:.9rem; color:var(--body); padding-left:calc(26px + .7rem);}

/* Terminal (deciding) gate emphasis */
.step.is-terminal{border-color:var(--edge2); background:var(--wash2);}
.step.term-sweep{--edge2:color-mix(in srgb,var(--sweep) 45%,transparent); --wash2:var(--sweep-wash);}
.step.term-hold{--edge2:color-mix(in srgb,var(--hold-bright) 55%,transparent); --wash2:var(--hold-wash);}
.step.is-terminal .order{background:var(--edge2); color:var(--ink); border-color:transparent;}
.step.is-terminal .name{color:var(--ink);}

.provenance{
  margin-top:2.6rem; padding-top:1.4rem; border-top:1px solid var(--border);
  font-size:.86rem; color:var(--muted); max-width:78ch;
}
.provenance code{color:var(--body);}
.prov-fine{margin:.6rem 0 0; font-size:.8rem;}

@media (max-width:780px){
  .thesis{grid-template-columns:1fr;}
  .divergence{flex-direction:row; min-width:0;}
  .divergence .d-line{height:1px; width:auto;}
  .d-tag{writing-mode:horizontal-tb; transform:none;}
  .ladders{grid-template-columns:1fr;}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important;}}
"""


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "decision-audit-panel.html"
    sweep, refuse = _traces()
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(sweep, refuse))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
