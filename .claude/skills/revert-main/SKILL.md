---
name: revert-main
description: "FORCE-MODE: git revert HEAD on main, run the suite, push directly. Needs TERMINAL_FORCE_MAIN=1 (hook-enforced). Use on /revert-main or 'revert main' / 'undo the last commit' / 'roll back HEAD'."
---

# /revert-main — undo the most recent main commit

`/revert-main` is the narrowest FORCE-MODE skill. It does exactly one thing:
`git revert --no-edit HEAD` on main, runs the suite, pushes. Anything more
than a one-commit revert belongs in `/emergency-fix` or `/unblock-ci`.

## [1] Authority

Same as `/emergency-fix`: requires `TERMINAL_FORCE_MAIN=1` in the env, set by
TerMinal's `revert-main` agent or a deliberate manual launch. Without it the
global block-main-merge hook refuses the push.

## [2] Refusal cases

Refuse and surface a human-readable reason if:
- HEAD is already a revert commit (would be a no-op or destructive).
- HEAD is more than 24h old — the team has likely built on top of it and a
  straight revert may break work-in-flight. Page humans instead.
- The most recent commit is on a non-default branch.
- The working tree is dirty.

## [3] Sequence

```
   1. Confirm you're on the latest main; capture HEAD SHA + subject
        │
        ▼
   2. `git revert --no-edit HEAD`
        │
        ▼
   3. Run the project test suite; confirm green
        │  (if red, abort and file a critical ticket — the revert itself
        │   broke something; don't paper over it)
        ▼
   4. `git push origin main`     ← hook bypassed via TERMINAL_FORCE_MAIN=1
        │
        ▼
   5. File a follow-up ticket
        │  title: "Re-do reverted: <original subject>"
        │  type: bug  ·  priority: high  ·  source: revert-main
        │  assign exactly one owner agent via list_agents
        │  body must include: reverted SHA, reason, fix prompt for /factory
        ▼
   6. Hand off with reverted SHA + new main SHA + ticket id
```

## [4] When NOT to use

- More than one commit needs reverting. (→ `/emergency-fix` with a deliberate
  multi-commit revert plan, or page humans.)
- The bad commit is several commits back and reverting it would conflict.
  (→ `/emergency-fix` with a forward-patch.)
- The revert touches data migrations or schema changes. NEVER auto-revert
  those — surface a critical ticket and stop.
