---
name: emergency-fix
description: "FORCE-MODE hotfix: smallest patch → commit → push DIRECTLY to main → follow-up ticket. Needs TERMINAL_FORCE_MAIN=1 (hook-enforced). Use on /emergency-fix or 'this is breaking prod' / 'hotfix' / 'we need to revert now'."
---

# /emergency-fix — production hotfix bypass

`/emergency-fix` is the one and only skill that intentionally bypasses the
human-merge gate on `main`/`master`. It is for **production-impacting** bugs
that are hurting real users RIGHT NOW. Every other change goes through
`/pr-creation` → `code-review` agent → human merge.

## [1] Authority

The skill ONLY works when the caller is launched with
`TERMINAL_FORCE_MAIN=1` in the environment. Without it, the global
`~/.claude/hooks/block-main-merge.sh` PreToolUse hook will refuse the
final `git push origin main`. Three legitimate ways to be authorized:

1. **TerMinal's `emergency-fix` / `unblock-ci` / `revert-main` agents** —
   the runner injects the var per-spawn.
2. **A deliberate manual launch:** `TERMINAL_FORCE_MAIN=1 claude` from
   the shell, with a written reason in your scrollback. Do NOT export
   the var in `~/.zshrc` — that's how accidents happen.
3. **autopilot-harness** — the harness's own repo is already exempt
   from the hook by path; the force-mode mechanism is irrelevant there.

If you are NOT in one of those three contexts, this skill is the wrong
tool — use `/pr-creation` instead.

## [2] Sequence

```
   1. Reproduce the failure (briefly — confirm scope before patching)
        │
        ▼
   2. Write the SMALLEST surgical change that stops the bleeding
        │  · no refactor · no cleanup · no scope expansion
        ▼
   3. Run the relevant test subset; confirm green
        │
        ▼
   4. Commit on main with `fix: <subject>` (Conventional Commits)
        │
        ▼
   5. `git push origin main`     ← hook bypassed via TERMINAL_FORCE_MAIN=1
        │
        ▼
   6. File a backlog ticket capturing real root-cause + proper fix prompt
        │  · type: bug · priority: high · source: emergency-fix
        ▼
   7. Hand off with the SHA you pushed + ticket id
```

## [3] Bar — when NOT to use

- The bug is not actually breaking production. (→ `/pr-creation`)
- The minimal patch isn't obvious within ~5 minutes of investigation.
  Page humans instead — a wrong hotfix is more expensive than a paged
  on-call. File a critical ticket and stop.
- The fix touches more than one logical change. Split it.
- You're tempted to "also clean up X while I'm here." No. Surgical.

## [4] What gets committed

- The minimal source change (one file is the norm; two is the cap).
- A test that pins the regression, IF the smallest-possible-test fits
  into the same patch. If a real test takes a refactor to write, ship
  the fix now and file the test as the follow-up ticket.

## [5] What goes in the follow-up ticket

- **Title:** "Proper fix: <brief restatement of root cause>"
- **type:** bug / **priority:** high / **source:** emergency-fix
- **owner:** exactly one assigned agent (`agent_id`, `agent_scope`,
  `agent_kind`) selected with `list_agents`
- **Body must include:**
  - The SHA pushed to main as the emergency patch
  - Why the patch is a stopgap (what it papered over vs what's still broken)
  - A self-contained agent-runnable fix prompt for `/factory` to take
  - Any test coverage gap the emergency revealed

## [6] Related FORCE-mode skills (also bypass main)

- `/unblock-ci` — main CI is red blocking the team; narrow revert/pin/skip.
- `/revert-main` — narrowly `git revert HEAD` on main and push.

All three flow through the same `TERMINAL_FORCE_MAIN=1` carve-out. The
authority is the same; the playbook differs.
