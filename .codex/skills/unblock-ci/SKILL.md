---
name: unblock-ci
description: "FORCE-MODE: main CI is red — apply the narrowest of revert bad SHA / pin dep / skip documented-flaky test, pushed directly to main. Needs TERMINAL_FORCE_MAIN=1. Use on /unblock-ci or 'main CI is broken' / 'green main'."
---

# /unblock-ci — get main green so the team can ship

`/unblock-ci` is a FORCE-MODE skill (see `/emergency-fix` for the full authority
explanation). It bypasses the human-merge gate ONLY for the narrowest possible
fix that returns the default-branch CI to green.

## [1] Authority

Same as `/emergency-fix`: requires `TERMINAL_FORCE_MAIN=1` in the env, set by
TerMinal's `unblock-ci` agent or a deliberate manual launch. Without it the
global block-main-merge hook refuses the push.

## [2] Classify before you fix

Pull the failing run's logs (`gh run view --log` / `glab ci view`) and
classify the failure:

| Class | Symptom | Narrowest fix |
|-------|---------|---------------|
| (a) Regression from the last merge | new failure on first run after a known commit | `git revert <bad-sha>` |
| (b) Flapping test | same test fails intermittently across reruns | mark with project skip/retry convention |
| (c) Dep / install break | `npm install` / `bun install` / `pip install` fails | pin the last-working version |
| (d) Infra | runner died, registry down, network out | **NO fix** — file a ticket and exit |

If the failure is genuinely outside the fix budget of those four classes,
**file a ticket and stop**. Do not paper over a real bug.

## [3] Sequence

```
   1. Pull failing CI logs; classify (a/b/c/d above)
        │
        ▼
   2. Apply the narrowest fix matching the class
        │  (a) git revert <bad-sha>
        │  (b) mark test skip/retry per project convention
        │  (c) pin the dep
        │  (d) exit — file ticket only
        ▼
   3. Run the project's test suite locally; confirm green
        │
        ▼
   4. Commit on main; push directly (`git push origin main`)
        │
        ▼
   5. File a follow-up ticket
        │  type: testing | ci | dependency  ·  priority: high
        │  assign exactly one owner agent via list_agents
        ▼
   6. Hand off with the action taken + new main SHA + ticket id
```

## [4] Bar — when NOT to use

- The CI is yellow (slow, not red).
- The failure is on a feature branch.
- The "fix" requires touching more than the failing test/dep/revert. Split.
- You'd be rolling forward an unrelated change. Forbidden.
