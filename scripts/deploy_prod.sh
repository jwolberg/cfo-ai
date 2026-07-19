#!/usr/bin/env bash
# Stage 3 of ticket 0057: deploy the identity + link + demo-session API to Cloud Run — the cutover
# from the pre-identity shared-key revision to the Stytch one. Idempotent. Reads secrets from the
# macOS Keychain; nothing sensitive is written to disk or argv.
#
# Prereqs (once): the two Keychain entries below must exist —
#   cfo-ai-stytch-secret          (already present)
#   cfo-ai-demo-stytch-password   (store with: security add-generic-password -s cfo-ai-demo-stytch-password -U -w '<pw>')
#
# This REPLACES the live demo's API. Roll back with:
#   gcloud run services update-traffic resfi-api --to-revisions=resfi-api-00003-viv=100 --region us-central1 --project cfo-ai-1
set -euo pipefail
cd "$(dirname "$0")/.."

PROJ="cfo-ai-1"
REGION="us-central1"
SERVICE="resfi-api"
SA="613154826987-compute@developer.gserviceaccount.com"

echo "== preflight =="
echo "gcloud account: $(gcloud config get-value account 2>/dev/null)"
echo "gcloud default project (NOT used; we pass --project=$PROJ explicitly): $(gcloud config get-value project 2>/dev/null)"
echo "cwd: $(pwd)"
echo "demo route in tree: $(grep -c '"/demo/session"' backend/main.py)"

ensure_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" --project "$PROJ" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project "$PROJ" >/dev/null
    echo "secret $name: new version added"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --project "$PROJ" >/dev/null
    echo "secret $name: created"
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor \
    --project "$PROJ" >/dev/null
}

echo "== ensuring Secret Manager entries =="
ensure_secret stytch-secret "$(security find-generic-password -s cfo-ai-stytch-secret -w)"
ensure_secret demo-stytch-password "$(security find-generic-password -s cfo-ai-demo-stytch-password -w)"

echo "== deploying service='$SERVICE' project='$PROJ' region='$REGION' (Cloud Build; ~3-5 min) =="
# All flags use the '=' form and --quiet so gcloud never prompts and never falls back to the active
# config project/dir-name service (which is what sent an earlier attempt to the wrong project).
# --set-secrets/--set-env-vars REPLACE those sets, so both list the FULL desired set. The instance
# pin is load-bearing (the in-process Anthropic rate cap, backend/assistant.py). The env delimiter is
# '|' (^|^) because a value has commas (origins) and another an '@' (email).
gcloud run deploy "$SERVICE" \
  --source=. \
  --project="$PROJ" \
  --region="$REGION" \
  --quiet \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=1 \
  --set-secrets=RESFI_API_KEY=resfi-api-key:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,DATABASE_URL=resfi-database-url:latest,STYTCH_SECRET=stytch-secret:latest,DEMO_STYTCH_PASSWORD=demo-stytch-password:latest \
  --set-env-vars='^|^PYTHONUNBUFFERED=1|RESFI_ALLOWED_ORIGINS=https://cfo-ai-1.web.app,https://cfo-ai-1.firebaseapp.com|STYTCH_PROJECT_ID=project-test-2f753ee4-ae25-4a19-a3c2-b1db648b3c1f|PLAID_ENV=sandbox|DEMO_STYTCH_EMAIL=demo-viewer@example.com'

echo "== deployed. verifying =="
URL="$(gcloud run services describe "$SERVICE" --project "$PROJ" --region "$REGION" --format='value(status.url)')"
echo "service URL: $URL"
echo -n "POST /demo/session -> "; curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/demo/session"
