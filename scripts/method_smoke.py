"""Connect to Method and exercise the payoff-leg client — a runnable smoke test (ticket 0044).

Proves the live `MethodHttpClient` (`backend/transfer/method_client.py`) actually talks to Method,
before the hard gate (`tests/test_transfer_sandbox.py`) drives it through the whole saga. Reads its
config from the environment so no secret is ever committed or passed on the command line:

    METHOD_API_KEY        (required)  your Method secret key, sk_...
    METHOD_ENV            (default dev)  dev | sandbox | production   — use dev (mocked) to start
    METHOD_SOURCE_ID      (optional)  a platform funding account id to pay FROM (acc_...)
    METHOD_DESTINATION_ID (optional)  a liability (card) account id to pay TO   (acc_...)
    METHOD_RUN_LIVE_DEV   (optional)  set to 1 to actually create + simulate a dev payment

Run:  METHOD_API_KEY=sk_... .venv/bin/python scripts/method_smoke.py

Steps, each reported:
  1. connectivity — an authenticated GET /entities (needs only the key). A bad key fails here.
  2. dry-run payment — if source+destination given, POST /payments dry_run (validates, moves $0).
  3. live dev flow — if METHOD_RUN_LIVE_DEV=1 and env=dev: create a real (mocked) payment,
     simulate it to `sent` then `posted`, and read the status back. Dev only — money is mocked.

The key is never printed. This script is a dev tool; it is not imported by the app.
"""

from __future__ import annotations

import os
import sys

from backend.transfer.method_client import MethodHttpClient


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"missing required env var {name} (see this script's docstring)")
    return value


def main() -> None:
    api_key = _need("METHOD_API_KEY")
    env = os.environ.get("METHOD_ENV", "dev")
    source = os.environ.get("METHOD_SOURCE_ID")
    destination = os.environ.get("METHOD_DESTINATION_ID")
    run_live = os.environ.get("METHOD_RUN_LIVE_DEV") == "1"

    print(
        f"→ Method {env} ({MethodHttpClient.__module__}); key present: {api_key[:3]}… (not shown)"
    )

    with MethodHttpClient(api_key, env=env) as client:
        # 1 — connectivity / auth
        try:
            code = client.ping()
        except Exception as exc:  # noqa: BLE001 — a smoke test reports any failure verbatim
            sys.exit(f"✗ connectivity failed (bad key or wrong env?): {exc}")
        print(f"✓ connected — GET /entities returned {code}")

        # 2 — dry-run payment (validates the payment path without moving anything)
        if source and destination:
            payment_id = client.create_payment(
                source_id=source,
                destination_id=destination,
                amount_cents=100,  # $1.00 — under the sandbox per-txn cap too
                description="RESFI",
                idempotency_key="smoke-dry-run",
                dry_run=True,
            )
            print(f"✓ dry-run payment validated — {payment_id}")
        else:
            print("• skipped payment: set METHOD_SOURCE_ID + METHOD_DESTINATION_ID to exercise it")
            return

        # 3 — a real (mocked, in dev) payment driven through settlement
        if not run_live:
            print("• skipped live dev flow: set METHOD_RUN_LIVE_DEV=1 (dev only) to run it")
            return
        if env != "dev":
            sys.exit("refusing the live flow outside dev — money is only mocked in dev")

        payment_id = client.create_payment(
            source_id=source,
            destination_id=destination,
            amount_cents=100,
            description="RESFI",
            idempotency_key="smoke-live-dev",
        )
        print(
            f"✓ created dev payment {payment_id} — status {client.get_payment(payment_id).status}"
        )
        for status in ("sent", "posted"):
            client.simulate_payment_status(payment_id, status)
            print(f"  → simulated {status}; now {client.get_payment(payment_id).status}")
        print("✓ dev payoff flow reached posted")


if __name__ == "__main__":
    main()
