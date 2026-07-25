"""Sync .TerMinal/backlog/ (the git-ignored TerMinal-cockpit mirror) to docs/tickets/
(the git-tracked source of truth). See docs/tickets/README.md for why two stores exist.

docs/tickets wins on every shared field (title, status, type, priority, source,
depends_on, created, body). Harness-only fields (updated, prs, refs, acceptance,
model_tier, horizon, worked_by) are preserved from the existing mirror when present,
with sensible defaults for a brand-new mirror file. Idempotent and safe to re-run.

Usage:  python3 scripts/sync-backlog-mirror.py
Run from anywhere — paths resolve relative to the repo root, not the cwd.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "tickets"
DST = ROOT / ".TerMinal" / "backlog"

TYPE_MAP = {  # docs vocab -> harness vocab (bug|dx|feature|security|testing)
    "fix": "bug", "bug": "bug",
    "feat": "feature", "feature": "feature",
    "chore": "dx", "dx": "dx",
    "security": "security", "testing": "testing",
}
STATUS_MAP = {"done": "closed", "closed": "closed", "open": "open",
              "in-progress": "in-progress"}


def parse(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, m.group(2)


def uq(v: str) -> str:
    v = (v or "").strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def deps(v: str) -> str:
    return "[" + ", ".join(str(int(n)) for n in re.findall(r"\d+", v or "")) + "]"


def run() -> list[str]:
    changed = []
    for sp in sorted(SRC.glob("[0-9][0-9][0-9][0-9]-*.md")):
        s, body = parse(sp.read_text())
        dp = DST / sp.name
        d, _ = parse(dp.read_text()) if dp.exists() else ({}, "")

        num = int(uq(s["id"]))
        created = uq(s.get("created", ""))
        # harness-only fields: keep the mirror's value, else default
        updated = uq(d.get("updated", "")) or created
        model_tier = uq(d.get("model_tier", "")) or "auto"
        horizon = uq(d.get("horizon", "")) or "now"

        def keep_list(field: str) -> str:
            v = (d.get(field, "") or "").strip()
            return v if v.startswith("[") and v != "[]" else "[]"

        out = f"""---
id: {num}
title: "{uq(s.get('title','')).replace('"', "'")}"
status: {STATUS_MAP.get(uq(s.get('status','open')), 'open')}
priority: {uq(s.get('priority','medium'))}
horizon: {horizon}
type: {TYPE_MAP.get(uq(s.get('type','feature')), 'feature')}
source: {uq(s.get('source',''))}
created: {created}
updated: {updated}
prs: {keep_list('prs')}
refs: {keep_list('refs')}
depends_on: {deps(s.get('depends_on',''))}
acceptance: {keep_list('acceptance')}
model_tier: {model_tier}
worked_by: {keep_list('worked_by')}
agent_id: {uq(s.get('agentId','backend-python-agent'))}
agent_scope: {uq(s.get('agentScope','repo'))}
agent_kind: {uq(s.get('agentKind','classic'))}
---
{body if body.startswith(chr(10)) else chr(10) + body}"""

        old = dp.read_text() if dp.exists() else None
        if old != out:
            dp.write_text(out)
            changed.append(sp.name[:4])
    return changed


if __name__ == "__main__":
    ch = run()
    print(f"{len(ch)} mirror files updated: {' '.join(ch) if ch else '(none — already in sync)'}")
