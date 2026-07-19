"""Mint a real Stytch session_jwt for a throwaway sandbox user — the token web/link.html needs.

Local dogfooding only. Mirrors tests/test_identity_sandbox.py:_mint_session (create-or-authenticate
via the Passwords product) so a re-run authenticates the same user rather than erroring. Needs
STYTCH_PROJECT_ID + STYTCH_SECRET in the environment (pull STYTCH_SECRET from the Keychain).

    STYTCH_PROJECT_ID=project-test-... \
    STYTCH_SECRET="$(security find-generic-password -s cfo-ai-stytch-secret -w)" \
      .venv/bin/python scripts/mint_session.py [email]

Prints two lines: the session_jwt, then the Stytch user_id. Paste the JWT into link.html.
"""

from __future__ import annotations

import os
import sys

from stytch import Client

_PASSWORD = "dogfood-correct-horse-battery-staple-9x!"


def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else "dogfood@example.com"
    client = Client(
        project_id=os.environ["STYTCH_PROJECT_ID"],
        secret=os.environ["STYTCH_SECRET"],
        environment="test",
        suppress_warnings=True,
    )
    try:
        resp = client.passwords.create(
            email=email, password=_PASSWORD, session_duration_minutes=180
        )
    except Exception:  # noqa: BLE001 — existing user: authenticate instead of create
        resp = client.passwords.authenticate(
            email=email, password=_PASSWORD, session_duration_minutes=180
        )
    print(resp.session_jwt)
    print(resp.user_id, file=sys.stderr)


if __name__ == "__main__":
    main()
