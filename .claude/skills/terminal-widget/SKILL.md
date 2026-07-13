---
name: terminal-widget
description: "Add or edit repo-specific TerMinal sidebar widgets (shell commands the terminal polls) via .TerMinal/widgets.json. Use on /terminal-widget or to surface repo state (counts, status, metrics) in the sidebar."
---

# /terminal-widget — Repo-specific cockpit sidebar widgets

The TerMinal discovers per-repo sidebar widgets from a standardized
file at the repo root:

```
<repo-root>/.TerMinal/widgets.json
```

It's a JSON **array** of command widgets. The terminal polls each widget's shell
command (in the repo cwd) on its interval and renders the stdout in the sidebar.
This is the documented per-repo extension point — use it to surface
repo-specific state (open tickets, build status, queue depth, deploy version, …).

> **Tabs / React plugins are built-in only** — there is no per-repo discovery for
> new tabs yet. If a repo genuinely needs a custom tab, that's a change to the
> terminal core (propose it there); per-repo, command widgets are the surface.

## Widget schema

```jsonc
[
  {
    "id": "open-tickets",          // unique id (optional; auto-generated if omitted)
    "title": "Open",               // sidebar label (required)
    "icon": "🎫",                  // emoji/symbol (optional; default "▸")
    "command": "…",                // shell command, run in repo cwd (required)
    "intervalMs": 5000,            // poll cadence (default 5000; min 1)
    "mode": "big"                  // "text" | "big" | "kv"
  }
]
```

- **`mode: "big"`** — first line of stdout rendered as a large number/value
  (best for counts).
- **`mode: "text"`** — raw stdout, truncated (best for short status lines).
- **`mode: "kv"`** — `key: value` lines rendered as rows.

## Constraints (the terminal enforces)

- Command runs with a **6s timeout** and **256 KB** output buffer — keep it fast
  and small. Heavy work → write to a cache file on a cron and have the widget
  `cat` it.
- Stdout is **escaped plain text** — no HTML/markup rendering.
- **No sandbox.** Widgets run arbitrary shell in the repo cwd (same trust as
  npm scripts) — don't put secrets in commands; only attach trusted repos.

## Process

1. Read `.TerMinal/widgets.json` if it exists (else start a new array).
2. Append/edit a widget object per the schema. Keep `command` a one-liner that
   prints a tiny result fast; pick `mode` by shape (count→big, status→text,
   pairs→kv).
3. Write the file (valid JSON — it's an array, no trailing commas).
4. Tell the user it's live on the next poll (the terminal re-reads per interval);
   no restart needed for widget changes.

## Good repo-specific widgets to offer

Lean on this template's own bins so the cockpit shows workflow state:

- **Open tickets** (big): `.claude/skills/ticket/bin/tickets open 2>/dev/null | tail -n +3 | wc -l | tr -d ' '`
- **Needs you / HITL** (big, global inbox): `python3 -c "import json,os;p=os.path.expanduser('~/.config/TerMinal/hitl.json');print(sum(1 for h in (json.load(open(p)) if os.path.exists(p) else []) if h.get('status')=='open'))" 2>/dev/null || echo 0`
- **Active session** (text): `.claude/skills/session-start/bin/sessions active 2>/dev/null | tail -n +3`
- **Git** (text): `git status -sb | head -6`

Then add whatever is genuinely repo-specific (dev-server health, queue depth,
row counts, deploy version, eval pass-rate from a cached file, …).
