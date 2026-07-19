---
id: "0057"
title: A deployed web app that safely shows a real linked household — what production needs
type: feature
status: open
priority: medium
repo: cfo-ai
agentId: mobile-rn-agent
agentKind: classic
agentScope: repo
depends_on: ["0054", "0055"]
created: 2026-07-18
---

# A deployed web app that safely shows a real linked household — what production needs

Spawned from dogfooding the link→decision loop locally (2026-07-18). The mobile app now renders a
linked household's live decision **in dev** (`feat(mobile): render a linked household's live
decision`; `getLiveDecision` + a `linked` path in `Dashboard`, driven by `expo start --web` against
the local API). The obvious next step — "deploy it and see the same data in production" — is **not a
deploy command**. It is blocked on four independent things, three of them real safety/architecture
gaps, and this ticket records them so the work is a scoped decision, not a surprise.

Measured, not assumed (see `docs/implementation-notes.md`, 2026-07-18):

## The blockers

1. **The web session is a 5-minute dead end.** A Stytch `session_jwt` lives **exactly 5 minutes**
   (decoded: `exp − iat = 300s`) while the session itself lasts hours, and **no refresh mechanism is
   built** — the "+ refresh" half of KTD-4 was never implemented. The web build bakes one static
   token (`EXPO_PUBLIC_DEMO_SESSION`, `mobile/src/api/session.ts`), so any deployed web session 401s
   after 5 minutes with no recovery. The local dev fix (`scripts/dev_auth_proxy.py`, re-mint + inject)
   is explicitly **not** a production answer — it hands full owner access to whoever reaches it.

2. **No web sign-in exists.** Ticket `0055` (status: open) scopes the Stytch Expo sign-in as
   *native-only*; the web build is designed to only ever carry the baked token. There is no code path
   for a web visitor to authenticate as an owner and reach their own household.

3. **Baking an owner token into a public build is a security hole (confirmed).** The backend trusts
   whatever token is presented — role/membership come from the token's own claims
   (`backend/identity/deps.py`), with **no concept of "this is the restricted demo credential."** The
   "two planes cannot cross" safety (KTD-10) holds only because the deploy operator bakes the specific
   read-only `stytch-demo-viewer` token. Baking a real **owner** `session_jwt` for a non-demo
   household into the public `cfo-ai-1.web.app` bundle would give every anonymous visitor full read +
   owner writes (`PATCH /policy`, `POST /attest`, Plaid link-exchange) to that household's real
   financial data.

4. **Production cannot hold this data.** Cloud Run has **no Plaid credentials** wired
   (`docs/runbooks/deploy.md` / `DEPLOY.local.md` never mention `PLAID_*`), and `PLAID_ENV=production`
   is **structurally blocked at startup** — `assert_plaid_tokens_safe_at_rest()` refuses to boot until
   KMS envelope encryption lands (`architecture.md [7.2]`, unbuilt). Only `sandbox` boots. The linked
   household's data lives in local Postgres, not Neon. Separately, the whole identity + Link rung is
   **undeployed** — prod is still the pre-identity revision (`resfi-api-00003-viv`, 2026-07-16), and
   the deploy runbook itself predicts redeploying `main` as-is breaks the live demo (`deploy.md
   [0.5]`).

## Scope (pick a lane — they are not equivalent)

- **A — real web sign-in.** Build the Stytch web sign-in (extends `0055` to web) **and** a token
  refresh (close KTD-4: store the `session_token`, add a refresh endpoint + a client interceptor that
  re-mints the `session_jwt` on expiry). This is the only path that lets a real user see *their own*
  data on a public site. Largest, and the correct one.
- **B — gated / private deploy.** Deploy behind auth (not `--allow-unauthenticated` / not public
  Firebase hosting) so baking a longer-lived owner token is not a public exposure. Smaller, but the
  5-min JWT + no-refresh still bites unless refresh is built.
- **C — demo-plane version.** Persist a Sandbox-derived decision as an `is_demo` household so the
  existing read-only `viewer` token shows it safely (`0057`-alt). Avoids the owner-token hole, but
  frames Sandbox data as "demo" and needs the feed's `window`/`summary` shape built for a single live
  decision (the mobile `LiveDecision` view skips the hero today).

For **any** lane that shows real Plaid data in prod: wire `PLAID_*` (sandbox) into Cloud Run, and
deploy the identity + Link rung (`0054` first, per its runbook).

## Acceptance criteria

- [ ] A deployed web build can show a linked household's live decision **without** a static owner
      token in a public bundle, and **without** the session dying after 5 minutes.
- [ ] The chosen lane's safety property is *technical*, not deploy-discipline — a wrong bake or an
      expired token fails closed, not open.
- [ ] If real Plaid data is shown: `PLAID_ENV=sandbox` creds are in Secret Manager, the identity +
      Link rung is deployed (`0054`), and the linked household's data is in Neon.
- [ ] The dev-only `scripts/dev_auth_proxy.py` is never on the deploy path.

## Notes

- This is a spawned ticket — dogfooding surfaced it; it is not in any plan. See
  `docs/implementation-notes.md` (2026-07-18) for the measurements.
- The local dev experience it grew out of is complete and committed; nothing here blocks that.
