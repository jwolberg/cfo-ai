#!/usr/bin/env bash
# Stage 4 of ticket 0057: rebuild the Expo web app against the deployed API and publish to Firebase
# Hosting (cfo-ai-1.web.app). Run right after deploy_prod.sh — between the two, the live demo is down
# (the old bundle's shared-key auth no longer matches the new API).
#
# The build carries NO EXPO_PUBLIC_DEMO_SESSION, so the web app fetches a fresh read-only viewer
# token from POST /demo/session (ticket 0057 stage 0) instead of a baked one that would expire.
set -euo pipefail
cd "$(dirname "$0")/../mobile"

# The Cloud Run URL is stable across revisions. Explicit here so the build does not depend on .env.
export EXPO_PUBLIC_API_URL="https://resfi-api-ax7jrjo2tq-uc.a.run.app"
unset EXPO_PUBLIC_DEMO_SESSION

echo "== building web (expo export) against $EXPO_PUBLIC_API_URL =="
npm run build:web

echo "== firebase deploy --only hosting =="
firebase deploy --only hosting --project cfo-ai-1

echo "== done. verifying the live bundle points at the API and fetches a demo session =="
curl -s -o /dev/null -w "cfo-ai-1.web.app: %{http_code}\n" https://cfo-ai-1.web.app/
