#!/usr/bin/env bash
# The fail-closed check for the public read-only demo, run against the DEPLOYED API.
#
# This is the check `docs/plans/2026-07-21-001-demo-read-only-public-demo-plan.md` owes before the
# demo is offered, and it is also the demo's own step 4 — the one run live, in a terminal, in front
# of the audience. It proves the claim the deploy actually makes: an anonymous visitor can read the
# demo plane and cannot write anything, anywhere.
#
# It needs NO secrets and no local config. Every credential it uses is one an anonymous visitor
# already has: a token from the unauthenticated `POST /demo/session`. That is the point — if this
# script needed a key, it would not be measuring what the public can do.
#
#   ./scripts/verify_demo_readonly.sh                      # the deployed API
#   ./scripts/verify_demo_readonly.sh http://localhost:8000 # a local one
#
# Exit 0 only when every expectation holds. Any mismatch prints FAIL and the exit code is 1.
#
# ⚠️ `POST /households` is checked against 403 (ticket 0058). Against a deployment that predates
# that fix it returns 201 and CREATES A REAL HOUSEHOLD owned by the demo viewer — which is the bug,
# and is exactly how it was found on 2026-07-21. The route is idempotent per user, so re-running
# this against an unfixed deployment returns the same household rather than accumulating rows; the
# cleanup is still a scoped two-row delete under the Neon owner string.
set -uo pipefail

API="${1:-https://resfi-api-ax7jrjo2tq-uc.a.run.app}"
FAILURES=0

# `%{http_code}` alone, so nothing about the body — which carries a household's real numbers — is
# ever printed by the check itself.
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

check() {
  local label="$1" want="$2" got="$3"
  if [[ "$got" == "$want" ]]; then
    printf '  \033[32mPASS\033[0m  %-46s %s\n' "$label" "$got"
  else
    printf '  \033[31mFAIL\033[0m  %-46s %s (wanted %s)\n' "$label" "$got" "$want"
    FAILURES=$((FAILURES + 1))
  fi
}

echo "== ${API} =="
echo
echo "-- the public demo session (unauthenticated, as any visitor gets it) --"
TOKEN="$(curl -s -X POST "$API/demo/session" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("session_jwt",""))' 2>/dev/null)"
if [[ -z "$TOKEN" ]]; then
  echo "  FAIL  POST /demo/session returned no session_jwt — cannot continue."
  echo "        A 503 here means the demo identity is unconfigured on this deployment"
  echo "        (DEMO_STYTCH_EMAIL / DEMO_STYTCH_PASSWORD / STYTCH_SECRET)."
  exit 1
fi
AUTH=(-H "Authorization: Bearer $TOKEN")
echo "  minted a session_jwt (${#TOKEN} chars)"

# The household the demo actually shows. Taken from the viewer's own membership list rather than
# hardcoded, so this keeps working when the demo household is re-imported under a new id.
H="$(curl -s "${AUTH[@]}" "$API/households" \
  | python3 -c 'import sys,json; hs=json.load(sys.stdin)["households"]; print(hs[0]["id"] if hs else "")' 2>/dev/null)"
if [[ -z "$H" ]]; then
  echo "  FAIL  GET /households listed nothing — the demo viewer has no memberships."
  exit 1
fi
COUNT="$(curl -s "${AUTH[@]}" "$API/households" \
  | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["households"]))' 2>/dev/null)"
echo "  the viewer can see $COUNT household(s); reading $H"
echo
echo "  Every id above must be a demo-plane household. A real one here means the demo"
echo "  identity has crossed into the real plane (ticket 0058) — check before continuing."

echo
echo "-- reads: the demo works --"
check "GET  /households"                200 "$(code "${AUTH[@]}" "$API/households")"
check "GET  /households/{h}/live-decision" 200 "$(code "${AUTH[@]}" "$API/households/$H/live-decision")"
check "GET  /households/{h}/spend"      200 "$(code "${AUTH[@]}" "$API/households/$H/spend")"
check "GET  /households/{h}/policy"     200 "$(code "${AUTH[@]}" "$API/households/$H/policy")"

echo
echo "-- writes: every one must be refused --"
check "PATCH /households/{h}/policy"    403 "$(code -X PATCH "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"buffer_floor":"1.00","max_sweep":"1.00","max_weekly_sweep":"1.00","min_days_between_sweeps":1,"blackout_dates":[]}' \
  "$API/households/$H/policy")"
check "POST  /households/{h}/attest"    403 "$(code -X POST "${AUTH[@]}" "$API/households/$H/attest")"
# The three routes with no household to authorize against — role is per-household, so these are
# gated on the identity instead (`current_real_user`, users.is_demo). Ticket 0058.
check "POST  /households  (0058)"       403 "$(code -X POST "${AUTH[@]}" "$API/households")"
check "POST  /plaid/link/token  (0058)" 403 "$(code -X POST "${AUTH[@]}" "$API/plaid/link/token")"
check "POST  /plaid/link/exchange (0058)" 403 "$(code -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"public_token":"public-sandbox-not-a-real-token"}' "$API/plaid/link/exchange")"
# The one user-keyed write route NOT behind `current_real_user`: it is refused a layer lower, by
# `_linkable_household` ("a real bank cannot be linked to a demo household"). Same 403, different
# reason — worth measuring precisely because the reason is the weaker of the two.
check "POST  /plaid/sync/now"           403 "$(code -X POST "${AUTH[@]}" "$API/plaid/sync/now")"

echo
echo "-- identity: the token is not a skeleton key --"
check "no token"                        401 "$(code "$API/households")"
check "tampered token"                  401 "$(code -H "Authorization: Bearer ${TOKEN}tampered" "$API/households")"
check "a household the viewer is not in" 403 "$(code "${AUTH[@]}" "$API/households/hh_not_a_member_of_this_one/policy")"

echo
if (( FAILURES == 0 )); then
  echo "ALL CHECKS PASSED — the deployed demo reads and cannot write."
  exit 0
fi
echo "$FAILURES CHECK(S) FAILED — do not present this deployment as read-only."
exit 1
