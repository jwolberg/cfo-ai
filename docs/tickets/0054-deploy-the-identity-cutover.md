---
id: "0054"
closed: 2026-07-18
title: Deploy the identity rung — the Stytch + demo-session cutover, without breaking the demo
type: dx
status: done
priority: high
repo: cfo-ai
agentId: infra-devops-agent
agentKind: classic
agentScope: repo
source: docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md
depends_on: ["0048", "0049", "0050", "0051", "0052", "0053"]
created: 2026-07-18
---

# Deploy the identity rung — the Stytch + demo-session cutover, without breaking the demo

The identity rung is built and green (`0046`–`0053`) but **not deployed**. A naive redeploy of `main`
breaks the **live** demo, because the cutover changed the deploy contract: every user route now
requires a verified Stytch session. This ticket is running that cutover safely. The full procedure is
already written — `docs/runbooks/deploy.md` `[0.5]` — so this is executing and verifying it, not
designing it.

## Why it is not a routine redeploy (the three failure modes)

Per `deploy.md [0.5]`, deploying the new code without the setup below breaks the live surface in ways
`/health` will not reveal:

- **No `STYTCH_PROJECT_ID`** → the first authed request → `StytchConfigError` → **500**, while
  `/health` (unauthenticated) reports the revision healthy.
- **Migrations `0011`–`0013` not applied** → the new routes read tables that don't exist;
  `_assert_migrated()` does not check them. **`0012` backfills then DROPs `policies`** — safe by
  design, but it runs.
- **Web build with no `EXPO_PUBLIC_DEMO_SESSION`** → the public demo (`cfo-ai-1.web.app`) sends no
  bearer and gets **401** everywhere.

## Scope

1. **Configure the Stytch test project** on the deployed service: store `STYTCH_SECRET` in Secret
   Manager, pass `STYTCH_PROJECT_ID` as an env var (see the amended `[5]` command — mind the
   `--set-env-vars` REPLACE trap). Leave `STYTCH_ENV` unset/`test`.
2. **Migrate Neon to head** (`[2]`) — `0011`–`0013` — as the owner against the direct host.
3. **Seed / verify** (`[3]`) the demo `viewer` + reviewer `owner` users, their memberships, and
   `is_demo` (migration `0011` backfills `is_demo`; the seeder creates the principals).
4. **Deploy the backend** (`[5]`) with the two Stytch vars wired; confirm it boots (all lifespan
   gates pass, including `assert_stytch_secret_safe_at_rest`).
5. **Mint the demo session and rebuild the web** (`[7]`): mint a read-only `viewer` `session_jwt` for
   the seeded demo user, set `EXPO_PUBLIC_DEMO_SESSION`, `npm run build:web` + `firebase deploy`.
6. **Verify end to end** — the deployed demo loads and reads a demo household with no login; a real
   sign-in (once `0055` lands) reaches its own household; `/health` and the internal routes unchanged.
7. **Update `deploy.md`** `last-verified` to the deploy date, and record the demo-session mint command
   (the one step the runbook currently flags as unproven).

## Acceptance criteria

- [x] Stytch secrets stored/wired; the service boots against them; an authed request no longer 500s.
      — *Measured 2026-07-21: `POST /demo/session` mints a real Stytch `session_jwt` in prod, and
      authed `GET /households`, `/live-decision`, `/spend`, `/policy` all return 200.*
- [ ] Migrations `0011`–`0013` applied to Neon; no already-seeded household lost its policy.
      — *Implied by the working authed reads (the identity tables exist and `/policy` answers), but
      not directly verified against Neon. Confirm before closing.*
- [x] The public demo (`cfo-ai-1.web.app`) works **with no login** on the pre-seeded `viewer` session.
      — *Measured 2026-07-21: the deployed web bundle carries no baked token and fetches
      `/demo/session`; the demo household's live decision and spend surface both render from prod.*
- [x] A non-member/invalid session is refused on the deployed API (the cutover's whole point).
      — *Measured 2026-07-21: a non-member `household_id` named directly is **403**; a missing token
      and a tampered token are both **401**.*
- [ ] `deploy.md` re-verified and dated; the demo-session mint command recorded.
      — *Not done, and the runbook is now actively wrong: it still records the live revision as
      `resfi-api-00003-viv` (2026-07-16), which the 0057 stage-3 deploy superseded.*

## State as of 2026-07-21 (measured, not assumed)

The cutover **shipped** with the 0057 read-only demo deploy (`0a8674f`, `6c0b0db`) — this ticket sat
`open` while three of its five ACs were already true in production. It stays open on two things: the
Neon migration confirmation, and the stale `deploy.md`. Note that the deployed cutover is **not**
fully sound — see `0058`: `POST /households` has no plane gate, so the demo viewer can create a real
household.

## Notes

- Overlaps the still-open `0026` (Neon + Cloud Run deploy path) and `0009` — this rung's delta rides
  on top of those. The exact demo-session mint mirrors `_mint_session` in `tests/test_identity_sandbox.py`
  (`0053`, now green).
- Do **not** flip `STYTCH_ENV=live` — that is a real-users trigger (KTD-4) with its own secret story.
