#!/usr/bin/env bash
# Boot the API locally against the throwaway Postgres, with every real secret pulled from the
# macOS Keychain at start (nothing plaintext lands on disk or in shell history). Local dogfooding
# of the Plaid Link -> live-decision loop; NOT a deploy path (that is docs/runbooks/deploy.md).
set -euo pipefail
cd "$(dirname "$0")/.."

export DATABASE_URL="postgresql+psycopg://cfo_live:cfo_live@127.0.0.1:55432/cfo_ai_live"
# The service refuses to start without these two; neither value calls anything (no vendor here).
export RESFI_API_KEY="local-not-real"
export ANTHROPIC_API_KEY="sk-ant-local-not-real"
# Identity rung — the test Stytch project (DEPLOY.local.md); secret from the Keychain.
export STYTCH_PROJECT_ID="project-test-2f753ee4-ae25-4a19-a3c2-b1db648b3c1f"
export STYTCH_ENV="test"
export STYTCH_SECRET="$(security find-generic-password -s cfo-ai-stytch-secret -w)"
# Plaid Sandbox — the connect-a-bank half.
export PLAID_ENV="sandbox"
export PLAID_CLIENT_ID="$(security find-generic-password -s cfo-ai-plaid-client-id -w)"
export PLAID_SECRET="$(security find-generic-password -s cfo-ai-plaid-secret -w)"

exec .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
