---
title: Deploy the API to Cloud Run, with the database
last-verified: never — see [0]
anchor: RB-deploy
---

# Deploy the API to Cloud Run, with the database

Tickets `0009` (the Cloud Run path — run and live since 2026-07-14) and `0026` (the database in
that path — **not yet run**). This is the graduation `DEPLOY.local.md` §12 has been owing since
`0009`: the shareable procedure, with the infrastructure values stripped.

**No secrets in this file, or in any file in this repo.** Real values live in `DEPLOY.local.md`
(gitignored) and in Secret Manager. Every `<placeholder>` below is filled from there.

**The database half of this has never been executed.** Steps `[2]`–`[5]` are written from the
code and from [`neon-provisioning.md`](./neon-provisioning.md), not from a run — which is why
`last-verified` says `never`. Bump it, and delete this paragraph, the first time it goes through
end to end.

---

## [0] What is true right now, before you start

Read this or the rest will confuse you.

| | |
|---|---|
| **Live revision** | serves the **pre-`0024` build** — one household, from the committed JSON file |
| **Its routes** | `/decisions`, `/decisions/{day}/explain`, `/spend`, `/assistant/message` |
| **`main`'s routes** | `/health`, `/households`, `/households/{id}/decisions`, `/households/{id}/spend`, `/assistant/message` |
| **Overlap** | `/assistant/message`, and nothing else |

So this is **not** a routine redeploy. Every client path changes, the service gains a hard
dependency on Postgres, and the mobile client on `main` calls routes the live revision does not
have. `/health` does not exist on the live revision either (`DEPLOY.local.md` §11) — this deploy is
the first one where Cloud Run's probe has something to hit.

`main` could not deploy at all until ticket `0033`: `backend/requirements.txt` carried no database
driver while `backend/main.py` has imported SQLAlchemy since `0024`. That is fixed, and CI's
`deployable` job now installs that manifest alone and imports the serving path, so it cannot rot
again. **It has still never been built into an image.**

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
| **Direct** | `ep-*.<region>.aws.neon.tech` | steps `[2]`, `[3]` — Alembic and the seeder |
| **Pooler** | `ep-*-pooler.<region>.aws.neon.tech` | step `[4]` — the running service |

---

## [2] Migrate Neon to head, as the **owner**, against the **direct** host

The Neon project was provisioned and migrated during `0020`. It predates migration `0004`
(`spend_projections`, ticket `0031`), so **the deployed code will not run against it as it
stands** — `_assert_migrated()` refuses to start when a table the read path reads is missing, which
is the guard working.

Check first rather than assume:

```bash
export DATABASE_URL='postgresql+psycopg://<owner>:<pw>@<direct-host>/<db>?sslmode=require'
.venv/bin/python -m alembic current      # expect 0003 or earlier
.venv/bin/python -m alembic upgrade head # -> 0004
```

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
printf %s 'postgresql+psycopg://cfo_runtime:<pw>@<pooler-host>/<db>?sslmode=require' \
  | gcloud secrets create "$SECRET_DATABASE_URL" --data-file=- --project "$PROJECT_ID"
```

> **The `printf %s` is load-bearing, not style.** `--data-file=-` stores stdin *verbatim*, and a
> trailing newline inside a connection string fails at connect time in a way that reads like a bad
> password rather than a bad newline. `DEPLOY.local.md` §3 hit exactly this with the Anthropic key.

Grant the runtime service account access if it is not already covered by the project-level binding
the other two secrets use:

```bash
gcloud secrets add-iam-policy-binding "$SECRET_DATABASE_URL" \
  --member="serviceAccount:$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
      --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')" \
  --role=roles/secretmanager.secretAccessor \
  --project "$PROJECT_ID"
```

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
  --set-secrets="RESFI_API_KEY=${SECRET_API_KEY}:latest,ANTHROPIC_API_KEY=${SECRET_ANTHROPIC}:latest,DATABASE_URL=${SECRET_DATABASE_URL}:latest" \
  --set-env-vars="PYTHONUNBUFFERED=1"
```

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
| `ModuleNotFoundError` | `backend/requirements.txt` drifted from `[api]` again — ticket `0033` |
| `RESFI_API_KEY` / `ANTHROPIC_API_KEY` | the secret is missing or the binding is not granted |

## [6] Verify

`/health` should answer for the first time ever — it does not exist on the pre-`0024` revision.

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
