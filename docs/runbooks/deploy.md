---
title: Deploy the API to Cloud Run, with the database
last-verified: 2026-07-16
partially-verified: 2026-07-25
anchor: RB-deploy
---

> **Scope of the 2026-07-25 pass.** Four corrections, each measured against the live service or the
> source, and each marked inline where it lands: `[0]`'s status table, `[0.5]`'s demo-session
> failure mode, `[0.5]` step 5, and `[5]`'s deploy command (which was missing three variables the
> live revision carries). **Everything else still dates from 2026-07-16 and was not re-checked** —
> `last-verified` is deliberately left at that date rather than advanced on the strength of a
> partial pass.

# Deploy the API to Cloud Run, with the database

Tickets `0009` (the Cloud Run path — run and live since 2026-07-14) and `0026` (the database in
that path — run 2026-07-16). This is the graduation `DEPLOY.local.md` §12 has been owing since
`0009`: the shareable procedure, with the infrastructure values stripped.

**No secrets in this file, or in any file in this repo.** Real values live in `DEPLOY.local.md`
(gitignored) and in Secret Manager. Every `<placeholder>` below is filled from there.

---

## [0] What is true right now, before you start

Read this or the rest will confuse you.

| | |
|---|---|
| **Live revision** | `resfi-api-00011-wex` — measured 2026-07-25 |
| **Its routes** | 12 on the app itself: `/health`, `POST /demo/session`, `GET`+`POST /households`, and under `/households/{id}`: `decisions`, `live-decision`, `spend`, `decisions/{day}/explain`, `policy` (`GET`+`PATCH`), `attest` — plus `POST /assistant/message`. Three routers are mounted alongside them (`plaid.webhook`, `plaid.link`, `plaid.sync`). |
| **Database** | Neon. Migrations run to `0016_operator_actions` in the repo — **check the deployed head, do not assume it** (`[2]`) |

> **The three rows above were stale until 2026-07-25**, and each had drifted the same way: written
> true, then left while the thing moved. They read `resfi-api-00003-viv`, five routes, and
> `0004 (head)`. Revision and routes are now measured; the database row is deliberately phrased as
> an instruction rather than a number, because it is the one a reader is most likely to act on and
> the one this file has no way to keep current.

**This is a routine redeploy now. It was not, once, and that is worth thirty seconds.** Until
2026-07-16 the live revision was `00002-zop`: one household, read out of the committed JSON file,
served over a flat `/decisions`. The deploy below replaced it wholesale — every client path
changed, the service took on a hard dependency on Postgres, and `/health` answered for the first
time in the repo's history. That migration is done. What follows is the procedure that did it, and
re-running it against `main` today is the ordinary case.

`main` could not deploy at all until PR #50: `backend/requirements.txt` carried no database driver
while `backend/main.py` has imported SQLAlchemy since `0024`, so the container failed at import
before the first request while 496 tests passed. That is fixed, and `tests/test_requirements.py`
now walks the imports from the AST and asserts the manifest agrees, so it cannot rot again.
`0033` proved the manifest imports on a GitHub runner; **the 2026-07-16 deploy proved the claim
`0033` could not** — that a buildpack produces a container which *starts*. Starting at all means
every lifespan gate passed against real Neon: `expected_key()`, `database_url()`,
`assert_rls_binds()`, `_assert_migrated()`, `build_client()`.

**Still unmeasured: the cold start.** `--min-instances=1` keeps one instance warm, which removes
the question rather than answers it — `[6a]` is the measurement, and `0026` stays open until
someone records the wall-clock. Do not infer it from the ~500ms brochure figure.

## [0.5] ⚠️ The identity rung changed the deploy contract — read before redeploying `main`

**A redeploy of `main` at or after the identity rung (tickets `0046`–`0053`,
[`../plans/2026-07-17-003-...`](../plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md))
is NOT the routine redeploy `[0]` describes.** Every user route now requires a **verified Stytch
session** — the shared `RESFI_API_KEY` is retired for them. Deploy the new code without the steps
below and the live demo breaks in a way `/health` will not show you:

- **No `STYTCH_PROJECT_ID`** → the first authed request hits `stytch.verify` → `StytchConfigError` →
  **500**. `/health` is unauthenticated, so Cloud Run reports the revision healthy while every real
  request fails — the exact "looks fine, serves errors" failure ADR-0004 [3.2] is about.
- **Migrations `0011`–`0013` not applied** → the new routes read tables that don't exist.
  `_assert_migrated()` does **not** check them (it lists only the read-path tables), so it will not
  catch this at startup — you find out per-request. **`0012` backfills then DROPS the mutable
  `policies` table** (its history moves to append-only `policy_events`; safe by design, but it runs).
- **API deployed without `DEMO_STYTCH_EMAIL` + `DEMO_STYTCH_PASSWORD`** → `POST /demo/session`
  returns **503** (`demo_session_unconfigured`), the web client gets no token, and the public demo
  (`cfo-ai-1.web.app`) gets **401** on every route. The demo is not unauthenticated: it runs on a
  pre-seeded, read-only `viewer` session (KTD-10) — but as of ticket `0057` that session is
  **minted server-side on demand**, not baked into the build.

  > **Corrected 2026-07-25.** This bullet used to read "web build with no
  > `EXPO_PUBLIC_DEMO_SESSION`". That was true before `0057` and is not true now, and it sends you
  > hunting for a build-time token that should not exist. The failure moved from the **build** to
  > the **API config**. Measured: `mobile/.env` deliberately has no `EXPO_PUBLIC_DEMO_SESSION`,
  > `mobile/src/api/session.ts:97` fetches `POST /demo/session` on web when nothing is baked, and
  > the live endpoint returns `200` with a real `session_jwt`.
  >
  > The env var still *works* as an override — `session.ts:90` prefers a baked token when one is
  > present — so it remains useful for a dev override or a pinned build. It is simply no longer
  > required, and a ~5-minute `session_jwt` baked at build time is exactly the expiry bug `0057`
  > was filed to fix.

**Do these, in this order:**

1. **Validate Stytch end-to-end FIRST.** Create a Stytch **test** project, then run
   `tests/test_identity_sandbox.py` green (ticket `0053`) with `STYTCH_PROJECT_ID` + `STYTCH_SECRET`
   set. This is what confirms the real session-JWT shape; the plan budgets for a vendor-reality
   correction here, and if there is one it lands in `backend/identity/stytch.py` **before** it can
   bite the deploy. Until `0053` is green, everything below is built against an unconfirmed shape.
2. **Store the Stytch secrets** in Secret Manager alongside the others (`[4]`): `STYTCH_PROJECT_ID`
   (needed to verify JWKS and as the audience) and `STYTCH_SECRET` (server API). Leave `STYTCH_ENV`
   unset/`test` for a test project — `assert_stytch_secret_safe_at_rest()` no-ops there; flip to
   `live` only once the secret is genuinely managed, or startup refuses (KTD-4).
3. **Migrate and seed** as usual (`[2]`, `[3]`): `alembic upgrade head` applies `0011`–`0013`; the
   seeder creates the demo `viewer` + reviewer `owner` users, their memberships, and sets `is_demo`
   (migration `0011` also backfills `is_demo=true` for the already-seeded households).
4. **Deploy the backend** (`[5]`) with the two new secrets wired (see the amended command there).
5. **Wire the demo identity, then rebuild the web** (`[7]`): set `DEMO_STYTCH_EMAIL` and
   `DEMO_STYTCH_PASSWORD` (the password from Secret Manager in prod) on the API revision, then
   `npm run deploy:web` from `mobile/` — which runs `expo export`, `copy:docs`, and
   `firebase deploy --only hosting` in one step. **No token is minted by hand and none is set on
   the web build**: `backend/identity/demo.py` mints the demo `viewer` session server-side, caches
   it process-wide, and re-mints within a minute of expiry, so N visitors cost ~one Stytch call per
   5-minute window.

   Confirm before rebuilding the web, since a 503 here is what turns into a 401 in the browser:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST "$API_URL/demo/session"   # expect 200
   ```

   The demo identity is a real Stytch user whose `stytch_user_id` the seeder reconciles onto the
   demo viewer row (`scripts/seed_demo_household.py`), so step 3 is its prerequisite.

Everything else in this runbook still applies. The sections below were last verified for the
pre-identity revision — treat their command bodies as correct and these deltas as the amendment.

---

## [1] Placeholders

From `DEPLOY.local.md` §1. Nothing here is a secret.

```bash
export PROJECT_ID="<gcp-project>"
export REGION="<region>"
export SERVICE="<cloud-run-service>"

export SECRET_API_KEY="<secret-name-for-the-app-key>"
export SECRET_ANTHROPIC="<secret-name-for-the-anthropic-key>"
export SECRET_DATABASE_URL="<secret-name-for-the-runtime-connection-string>"   # new, ticket 0026
```

Neon gives you **two hosts and they are not interchangeable** — see
[`neon-provisioning.md`](./neon-provisioning.md). Migrations take session-level locks that
pgbouncer's transaction pooling does not carry:

| | Host | Used by |
|---|---|---|
| **Direct** | `ep-<id>.<compute>.<region>.aws.neon.tech` | steps `[2]`, `[3]` — Alembic and the seeder |
| **Pooler** | `ep-<id>-pooler.<compute>.<region>.aws.neon.tech` | step `[4]` — the running service |

`-pooler` is appended to the **endpoint-id segment only**; everything from `<compute>` (`c-2`,
`c-11`, …) rightward is identical. Neon's console defaults to the *direct* host and to a bare
`postgresql://` scheme — both wrong here, and both silent. `neon-provisioning.md` has the full
list of ways the obvious string misleads.

---

## [2] Migrate Neon to head, as the **owner**, against the **direct** host

The Neon project was provisioned during `0020` — which created migration `0001` **and stopped
there**. It was still at `0001` when measured on 2026-07-16: `0002` (`cards.apr_source`), `0003`
(re-keying `accounts` and `cards` on `(household_id, id)`) and `0004` (`spend_projections`) were
all outstanding. So the deployed code would not have run against it — `_assert_migrated()` refuses
to start when a table the read path reads is missing, which is the guard working.

An earlier draft of this section said "predates migration `0004`" and expected `0003`. Both were
true-but-misleading: it was **three** migrations behind, not one, and `0003` is a primary-key
restructure rather than an additive step. Hence: check, do not assume.

```bash
export DATABASE_URL='postgresql+psycopg://<owner>:<pw>@<direct-host>/<db>?sslmode=require'
.venv/bin/python -m alembic current      # measure it; do not predict it
.venv/bin/python -m alembic upgrade head # -> 0004
```

> **Before running `0003` against a database with rows in it, stop.** It drops and recreates the
> primary keys on `accounts` and `cards`. It was safe here only because every table was empty (0
> rows — verified, not assumed), so there was no data to move. That emptiness is a fact about
> 2026-07-16, not a property of the migration: re-check it rather than inherit this conclusion.

**Applied 2026-07-16.** Neon reached `0004` — which was head *that day* — and was seeded (`[3]`);
Postgres is 18.4. The deploy itself (`[5]`) ran the same day, so every step in this file has been
executed at least once.

> The repo has since moved to `0016_operator_actions`. This paragraph is a dated record of one
> migration run, **not a statement of where the database is now** — the original read "Neon is now
> at `0004 (head)`", which stopped being true the next time anyone added a migration. Read the
> deployed head with `alembic current`; do not infer it from here.

The owner *should* own the schema — that is the one thing Neon's `BYPASSRLS` default role is right
for. It is a **migration credential and nothing else**; it never reaches the service.

> ⚠️ `0004` adds `spend_projections` and puts RLS on it, so the scoped-table count goes from **five
> to six**. If you have a check anywhere that says five, it is now wrong.

## [3] Seed the four archetypes, as the **owner**, against the **direct** host

`0026`'s acceptance criterion is *"the deployed demo serves the four archetypes"*, and nothing has
ever written them to Neon. Without this the service starts clean and every household 404s.

```bash
# still the owner + direct host from [2]
.venv/bin/python -c "
from backend.db.session import make_engine
from backend.seed import seed_all
for s in seed_all(make_engine()):
    print(f'{s.archetype:24} {s.household_id:28} days={s.days} sweeps={s.sweeps}')
"
```

Seeding as the **owner** and serving as `cfo_runtime` is the split production has: a seeder is an
admin task, the service is not. The seeder is idempotent by construction (the household is deleted
and rewritten), so re-running converges rather than accumulating.

Expect four households, 90 days each. It writes `decisions`, `snapshots`, current state, **and**
`spend_projections` (`0031`) in one transaction per household.

## [4] Store the **runtime** connection string in Secret Manager

`cfo_runtime` — created in [`neon-provisioning.md`](./neon-provisioning.md) `[3]`, neither
`SUPERUSER` nor `BYPASSRLS` — against the **pooler** host.

**Not the owner string.** `backend/db/session.py`'s `assert_rls_binds()` refuses to start under a
role that bypasses RLS, and Neon's default role does. Under it every household-scoped query returns
**every household** while the IDOR suite stays green. That guard is the reason this step is worth
being slow about.

```bash
# First time only. If the secret already exists, `create` fails with "already exists" and
# changes nothing — use `versions add` instead; the deploy pins :latest either way.
printf %s 'postgresql+psycopg://cfo_runtime:<pw>@<pooler-host>/<db>?sslmode=require' \
  | gcloud secrets create "$SECRET_DATABASE_URL" --data-file=- --project "$PROJECT_ID"

# Rotating, or correcting a bad value:
printf %s 'postgresql+psycopg://cfo_runtime:<pw>@<pooler-host>/<db>?sslmode=require' \
  | gcloud secrets versions add "$SECRET_DATABASE_URL" --data-file=- --project "$PROJECT_ID"
```

> **The `printf %s` is load-bearing, not style.** `--data-file=-` stores stdin *verbatim*, and a
> trailing newline inside a connection string fails at connect time in a way that reads like a bad
> password rather than a bad newline. `DEPLOY.local.md` §3 hit exactly this with the Anthropic key.

**Grant the runtime service account access. This is required, not conditional.** There is **no**
project-level `secretmanager.secretAccessor` binding in this project — verified 2026-07-16 — so
every secret carries its own. An earlier draft of this section said the other two secrets were
covered by a project-level binding and framed this step as "if it is not already covered"; that
was false, and the step got skipped on the strength of it. A secret the service account cannot
read is indistinguishable at boot from a secret that does not exist: the instance dies with an
error naming the **mount**, not the IAM binding.

```bash
gcloud secrets add-iam-policy-binding "$SECRET_DATABASE_URL" \
  --member="serviceAccount:$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
      --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')" \
  --role=roles/secretmanager.secretAccessor \
  --project "$PROJECT_ID"
```

Confirm all three, rather than trusting the grant landed:

```bash
SA="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
      --format='value(spec.template.spec.serviceAccountName)')"
for S in "$SECRET_API_KEY" "$SECRET_ANTHROPIC" "$SECRET_DATABASE_URL"; do
  gcloud secrets get-iam-policy "$S" --project "$PROJECT_ID" --format=json | grep -q "$SA" \
    && echo "  $S: readable" || echo "  $S: NOT READABLE — boot will fail"
done
```

### Verify the value **you stored**, not one you retype

Every check that matters reads the secret back out of Secret Manager, because the string in your
shell and the string in the secret are different objects and only one of them boots the service.
Echoes no secret:

```bash
gcloud secrets versions access latest --secret="$SECRET_DATABASE_URL" --project "$PROJECT_ID" \
  | grep -q '^postgresql+psycopg://cfo_runtime:' && echo 'shape ok' || echo 'WRONG — do not deploy'

DATABASE_URL="$(gcloud secrets versions access latest --secret="$SECRET_DATABASE_URL" \
  --project "$PROJECT_ID")" .venv/bin/python -c "
from sqlalchemy import create_engine, text
from backend.db.session import assert_rls_binds, database_url
with create_engine(database_url()).connect() as c:
    assert_rls_binds(c)
    print('connected as', c.execute(text('SELECT current_user')).scalar(), '| RLS binds')"
```

Expect `connected as cfo_runtime | RLS binds`. `RlsWouldNotBind` means you stored the owner
string; `ModuleNotFoundError: psycopg2` means the `+psycopg` prefix is missing.

**Done 2026-07-16**, against version 2 of the secret — version 1 held the pre-correction string
(direct host, no `+psycopg`, since-rotated password) and is stale. RLS was confirmed to bind
through the pooler, an unscoped read of `decisions` returned 0 rows, a scoped read returned only
its own household, and the scope did not survive the transaction.

## [5] Deploy

`DEPLOY.local.md` §5's command, plus `DATABASE_URL`. Via `--set-secrets`, never
`--set-env-vars`: a plain env var is visible in `gcloud run services describe` and in the
deployment history; a Secret Manager reference is not.

```bash
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=1 \
  --cpu=1 \
  --memory=512Mi \
  --timeout=120s \
  --set-secrets="RESFI_API_KEY=${SECRET_API_KEY}:latest,ANTHROPIC_API_KEY=${SECRET_ANTHROPIC}:latest,DATABASE_URL=${SECRET_DATABASE_URL}:latest,STYTCH_SECRET=${SECRET_STYTCH_SECRET}:latest,DEMO_STYTCH_PASSWORD=${SECRET_DEMO_STYTCH_PASSWORD}:latest" \
  --set-env-vars="^|^PYTHONUNBUFFERED=1|RESFI_ALLOWED_ORIGINS=https://cfo-ai-1.web.app,https://cfo-ai-1.firebaseapp.com|STYTCH_PROJECT_ID=${STYTCH_PROJECT_ID}|DEMO_STYTCH_EMAIL=${DEMO_STYTCH_EMAIL}|PLAID_ENV=sandbox"
```

> ⚠️ **Corrected 2026-07-25 — the command above was missing three variables the live service
> actually carries, and both flags REPLACE rather than merge.** Read as written, it would have
> dropped `DEMO_STYTCH_EMAIL`, `DEMO_STYTCH_PASSWORD` and `PLAID_ENV` from the next revision. The
> first two break the public demo (`POST /demo/session` → 503 → the browser gets 401 on every
> route, `[0.5]`); dropping `PLAID_ENV` changes which Plaid environment the service talks to.
>
> Measured directly against the live revision `resfi-api-00011-wex` on 2026-07-25 — this is the
> full set, and the split between secret and env-var is the deployed one, not a proposal:
>
> | Variable | Wired as | Note |
> |---|---|---|
> | `RESFI_API_KEY` | secret `resfi-api-key` | residual internal use; authorizes no user route |
> | `ANTHROPIC_API_KEY` | secret `anthropic-api-key` | the explain path |
> | `DATABASE_URL` | secret `resfi-database-url` | the **runtime** role string (`[4]`) |
> | `STYTCH_SECRET` | secret `stytch-secret` | server API |
> | `DEMO_STYTCH_PASSWORD` | secret `demo-stytch-password` | mints the demo viewer session |
> | `DEMO_STYTCH_EMAIL` | env-var | the demo identity's address; not a credential |
> | `STYTCH_PROJECT_ID` | env-var | JWKS issuer/audience; appears in every token |
> | `RESFI_ALLOWED_ORIGINS` | env-var | CORS |
> | `PLAID_ENV` | env-var (`sandbox`) | |
> | `PYTHONUNBUFFERED` | env-var (`1`) | |
>
> Before deploying, diff the command against reality rather than trusting this table — it is a
> snapshot of one day, and the failure mode is silent:
>
> ```bash
> gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
>   --format="value(spec.template.spec.containers[0].env)" | tr ';' '\n' | grep -oE "name': '[A-Z_]+'"
> ```

> **Identity-rung amendment (see `[0.5]`).** `STYTCH_SECRET` joins the secrets and
> `STYTCH_PROJECT_ID` joins the env-vars (the project id is not itself a secret — it is the JWKS
> issuer/audience and appears in every token). Store `SECRET_STYTCH_SECRET` in Secret Manager the
> same way as the others (`[4]`). Because `--set-env-vars` REPLACES the whole set (see the warning
> below), `STYTCH_PROJECT_ID` MUST be listed here alongside the CORS origins — a deploy that drops it
> is the `StytchConfigError` 500 from `[0.5]`. `RESFI_API_KEY` stays wired only for any residual
> internal use; it authorizes no user route now. Leave `STYTCH_ENV` unset (defaults `test`).

> **Transfer vendor keys (Method, Increase) — not wired yet, and here is where they go when they
> are.** The payoff-leg key (`METHOD_API_KEY`) and the debit-leg key follow `STYTCH_SECRET` exactly:
> stored in Secret Manager (`[4]`), added to `--set-secrets` (e.g.
> `METHOD_API_KEY=${SECRET_METHOD_API_KEY}:latest`), never in an env-var and never on a developer's
> machine — **locally they live in the macOS Keychain** (`local-development.md` → *Real vendor keys*),
> and the two stores never touch. They stay **out** of the deploy today on purpose: the rail is in
> shadow (`TRANSFER_MODE` unset → `shadow`), and `assert_transfer_credentials_safe_at_rest` *refuses
> a `TRANSFER_MODE=live` boot* until the Method key comes from a secrets manager **and** the debit
> credential is KMS-encrypted (both currently hardcoded off) — plus the FBO/funding accounts and the
> Reg E/GLBA/MTL work ADR-0006 names. So: add these secrets the day `TRANSFER_MODE=live` is turned
> on, not before. `METHOD_ENV` selects `dev`/`sandbox`/`production` (an env-var, not a secret); use
> `dev` everywhere until the sandbox gate (`tests/test_transfer_sandbox.py`, ticket 0044) runs green.

> ⚠️ **`--set-env-vars` REPLACES the whole set — it does not add to it.** The running service carries
> `RESFI_ALLOWED_ORIGINS` as well as `PYTHONUNBUFFERED`, and a deploy passing only the latter drops
> the CORS allowlist. The API keeps working; the **browser client silently stops being able to call
> it**, which reads as "the deploy broke the app" and is in fact one missing env var.
>
> `DEPLOY.local.md` sets both in one command for this reason, and it is why `RESFI_ALLOWED_ORIGINS`
> is repeated here rather than left to the CORS section — a deploy command that needs a *second*
> command to be correct is a deploy command that is wrong. (An earlier draft of this runbook copied
> §5 without it. Caught by diffing the live service's env against the command, not by deploying.)
>
> **The `^|^` prefix is load-bearing.** It tells `gcloud` to split on `|` instead of `,` — and
> `RESFI_ALLOWED_ORIGINS` is *itself* a comma-separated list. Without it, gcloud reads
> `https://cfo-ai-1.firebaseapp.com` as a second variable, and fails on a value with no `=` in it.
>
> Prefer `--update-env-vars` if you ever want to change one and leave the rest alone. This is a
> `--set` because the deploy should state the container's whole environment, not inherit half of it
> from whatever the last person ran.

`--max-instances=1` is not a throughput compromise — the assistant's rate cap lives in process
memory and is the only thing bounding Anthropic spend if the public API key leaks. One process, one
cap. See the `Procfile`.

**Migrations are not in this path, deliberately.** They run in `[2]`, by hand, before the deploy.
An entrypoint that migrates would have every cold start racing to take a schema lock, on a service
that scales — and `--max-instances=1` is a policy, not a guarantee. The startup guard is what makes
the manual step safe: `_assert_migrated()` refuses to serve against a schema that is missing a table
the read path reads, so a forgotten `[2]` is a failed deploy rather than a service answering 500s.

**If the deploy fails at startup, that is the design.** `backend/main.py`'s lifespan asserts the API
key, the Anthropic key, that the database is reachable and migrated, and that RLS *binds*. A
container that comes up holding bad data and answers with wrong numbers looks perfectly healthy to
Cloud Run; one that never comes up does not (ADR-0004 [3.2]). Read the logs before assuming a flake:

```bash
gcloud run services logs read "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --limit 50
```

| What you see | What it means |
|---|---|
| `RlsWouldNotBind` | you used the owner string in `[4]`, not `cfo_runtime` |
| `the database has no readable ...` | `[2]` did not run, or ran against the wrong branch |
| `ModuleNotFoundError` | `backend/requirements.txt` drifted from the imports — `tests/test_requirements.py` should have caught it |
| `RESFI_API_KEY` / `ANTHROPIC_API_KEY` | the secret is missing or the binding is not granted |

## [6] Verify

`/health` should answer. It first did on 2026-07-16 — it did not exist on the pre-`0024` revision,
so a `404` here means you are looking at a revision older than that deploy, not at a broken probe.

```bash
SERVICE_URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
  --region "$REGION" --format='value(status.url)')
KEY=$(gcloud secrets versions access latest --secret="$SECRET_API_KEY" --project "$PROJECT_ID")

curl -fsS "$SERVICE_URL/health"                                        # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' "$SERVICE_URL/households"     # 401 — auth is on
curl -fsS -H "X-API-Key: $KEY" "$SERVICE_URL/households" | python3 -m json.tool

# The four archetypes, and the per-card spend surface 0031 built.
for H in hh_demo_biweekly hh_semimonthly_portfolio hh_monthly_thin hh_apr_unreported; do
  curl -fsS -H "X-API-Key: $KEY" "$SERVICE_URL/households/$H/spend" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('$H', [c['card_id'] for c in d['cards']], d['totals']['held_back'])"
done

# The route that no longer exists. 404, not a redirect: it could only ever serve one household.
curl -s -o /dev/null -w '/spend -> %{http_code}\n' -H "X-API-Key: $KEY" "$SERVICE_URL/spend"
```

`hh_semimonthly_portfolio` must list **three** cards. If it lists one, you are looking at a
pre-`0031` revision.

### [6a] Measure the cold start — do not trust the brochure

`0026`'s acceptance criterion, and `USERS.md` §2 is why: the reviewer wants to see this work in
under a minute. Neon wakes in ~300–500ms *per the brochure*; the client gives up at 8s
(`REQUEST_TIMEOUT_MS`).

**`--min-instances=1` does not save you here.** It keeps the *container* warm — that was `0009`'s
fix for the same 8s budget — and a warm container in front of a **cold database** is a new version
of the same problem, with the budget already spent.

```bash
# Let Neon idle out first (~5 min), then:
time curl -fsS -H "X-API-Key: $KEY" "$SERVICE_URL/households/hh_demo_biweekly/decisions" > /dev/null
```

Record the number in `0026`. If it is anywhere near 8s, that is a finding, not a flake.

## [7] Point the clients at it

The mobile/web clients on `main` call `/households/{id}/...`. The live revision has none of those
routes, so **the deployed web client breaks the moment the API is the new revision and the client
is not** — and vice versa. `DEPLOY.local.md` §8/§8b carry both.

Deploy the API first (it is the one that 404s gracefully), then the web client, and do not leave a
long gap.

## [8] Rollback

`DEPLOY.local.md` §9. Cloud Run keeps revisions; route traffic back:

```bash
gcloud run services update-traffic "$SERVICE" --to-revisions=<previous-revision>=100 \
  --project "$PROJECT_ID" --region "$REGION"
```

**Rolling back the API does not roll back the database**, and it does not need to: migration `0004`
only *adds* `spend_projections`, and no earlier revision reads it. The old revision serves the
committed JSON file and does not open a connection at all, so it is indifferent to everything in
`[2]`–`[4]`. That is a property of this particular migration, not a general rule — check before
assuming it of the next one.
