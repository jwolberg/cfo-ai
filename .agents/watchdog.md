# watchdog / heartbeat (in-runner contract)

Unlike the other entries in this directory, watchdog is **not** a scheduled
agent you wire up via `/check <kind>`. It's baked directly into
**`bin/terminal-cron`** so it runs automatically at the start of every cron
invocation (and as a standalone `terminal-cron watchdog` one-shot you can
schedule on its own LaunchAgent if you want a dedicated fleet-pulse).

It exists because, with 8+ scheduled agents in flight, the most expensive
failure mode is *silent absence-of-output*: a runner that got killed mid-run
(terminal closed, OOM, launchd unloaded), or a LaunchAgent that stopped
firing entirely (plist drifted, system relaunch, etc.). Without something
that *expects* heartbeats, the operator only finds out hours or days later.

## What it does

Two passes, both cheap:

**1. Stale-run sweep.** Scans `~/.config/TerMinal/cron-runs/*.json` for any
record stuck at `status: running` for longer than 2 hours and finalizes it:

- marks the record `status: failed` with `error: "stale: runner exited without
  finalizing the run (swept by watchdog)"`
- updates the schedule's `lastStatus`
- files a global HITL item with a link to the run id, branch, and worktree
- files a backlog ticket on the affected repo (best-effort — only if the repo
  has a `.TerMinal/backlog/` folder, or a legacy `backlog/` folder) with `type: bug · priority: high`

**2. Cadence check** (one-shot mode only). For each enabled `interval`
schedule whose `lastRun` is more than 2× its cadence ago, file a HITL: the
LaunchAgent should have fired by now and didn't. Calendar/cron-spec schedules
are skipped (a "missed firing" doesn't generalize cleanly outside intervals).

## Where it runs

- **Every cron invocation** of any scheduled agent runs the stale-run sweep
  before doing its own work. Net effect: as long as *one* schedule fires, the
  whole fleet stays clean. No dedicated schedule required for v1.

- **`terminal-cron watchdog`** runs both passes (sweep + cadence). Wire it up
  via the Schedules tab if you want a dedicated hourly heartbeat — that way
  even if no other schedule fires, you still get the cadence check.

## Kill-switch / circuit-breaker

When the runner finalizes any run as `failed`, it checks the schedule's last
`CIRCUIT_BREAK_AFTER` (default 3) records. If they are *all* `failed`, the
schedule's id is added to `~/.config/TerMinal/agents/disabled.json` and a
HITL is filed (`Circuit broken · <agent>`). The next firing reads that file
at the very top of `runSchedule` and exits 0 immediately, without trying.

To re-enable: click the red `kill-switch · re-enable` chip on the schedule's
row in the Schedules tab. (Or delete the id from `disabled.json` directly.)

## Why this isn't a `.agents/<kind>.md` spec

The watchdog has no per-run worktree, no PR, no `reports/watchdog/<sha>.md`
artifact — it's runtime plumbing, not a scheduled work item. If you want a
visible artifact, schedule the `terminal-cron watchdog` one-shot via the
Schedules tab and it'll emit an `activity` event each tick
(`Watchdog · swept N stale · M overdue`); HITL items provide the only "loud"
output paths.
