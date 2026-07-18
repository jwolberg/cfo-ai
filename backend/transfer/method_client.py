"""MethodHttpClient — the live Method Payments client (ticket 0044, U6).

`backend/transfer/method.py` translates the sweep's payoff leg to Method's Payments API behind the
`MethodClient` Protocol, and every test fakes that seam. This is the real implementation the
Protocol described — the "live client lands in U6" the module docstring promised — so the
`MethodProvider` can run against Method's actual API instead of a fake.

**Environments** (`docs.methodfi.com`, verified 2026-07): `dev` (`https://dev.methodfi.com`, *all
data and payments mocked* — the safe place to exercise the flow), `sandbox`
(`https://sandbox.methodfi.com`, real money, $1/txn, whitelisted entities), `production`. Auth is a
Bearer `sk_…` key, carried here and **never logged** (KTD-7) — the guard
`assert_transfer_credentials_safe_at_rest` still forbids a live-money boot until the key comes from
a secrets manager; this client is what that key will eventually drive.

**One vendor-model note to reconcile on first real run** (the gate's provenance warned of this):
Method's create-payment docs list **no idempotency field or header**, which contradicts the KTD-3
research note in `method.py` ("a reused key replays the original response"). We send the derived key
as an `Idempotency-Key` header regardless — harmless if ignored, correct if honored — and flag it
here so the saga's per-vendor conflict handling can be confirmed against reality, not the note.
"""

from __future__ import annotations

from typing import Any

import httpx

from backend.transfer.method import MethodPaymentView

BASE_URLS = {
    "dev": "https://dev.methodfi.com",
    "sandbox": "https://sandbox.methodfi.com",
    "production": "https://production.methodfi.com",
}

# Pin the API version so a Method-side default change can't silently reshape a response under us.
# (Method's own examples send this header; the value is the dated version the client was built to.)
METHOD_VERSION = "2026-03-30"


class MethodConfigError(RuntimeError):
    """The client was asked for an environment that does not exist."""


class MethodApiError(RuntimeError):
    """A non-2xx from Method, carrying the **response body** — Method explains a 400 in the body
    (`{success:false, message:…}`), and `raise_for_status` throws that away. Surfacing it is the
    difference between a diagnosable smoke run and a bare status code."""

    def __init__(self, status_code: int, method: str, url: str, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"{status_code} {method} {url} — {body}")


class MethodHttpClient:
    """A live `MethodClient` (plus a dev-only status simulator). Satisfies the Protocol in
    `backend/transfer/method.py`; the `MethodProvider` cannot tell it from a fake."""

    def __init__(
        self,
        api_key: str,
        *,
        env: str = "dev",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = BASE_URLS.get(env)
        if base_url is None:
            raise MethodConfigError(
                f"env={env!r} is not a Method environment ({sorted(BASE_URLS)})."
            )
        self.env = env
        # The key rides the default Authorization header and is never included in a log line — the
        # httpx client logs no bodies or headers, and nothing here formats the key into a string.
        # `transport` is a test seam (an `httpx.MockTransport`); None in production is the network.
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Method-Version": METHOD_VERSION,
            },
            timeout=timeout,
            transport=transport,
        )

    # --- the MethodClient Protocol --------------------------------------------------

    def create_payment(
        self,
        *,
        source_id: str,
        destination_id: str,
        amount_cents: int,
        description: str,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> str:
        """`POST /payments` — from the platform `source` to the card `destination`. Returns the
        Method payment id (`pmt_…`). `dry_run` validates the payment without moving money."""
        body: dict[str, Any] = {
            "amount": amount_cents,
            "source": source_id,
            "destination": destination_id,
            "description": description,
        }
        if dry_run:
            body["dry_run"] = True
        resp = self._http.post("/payments", headers={"Idempotency-Key": idempotency_key}, json=body)
        return _data(resp)["id"]

    def get_payment(self, payment_id: str) -> MethodPaymentView:
        """`GET /payments/{id}` → the status and, on a failure/reversal, the error code."""
        data = _data(self._http.get(f"/payments/{payment_id}"))
        return MethodPaymentView(status=data["status"], error_code=_error_code(data))

    # --- dev-only helpers (not part of the Protocol) --------------------------------

    def simulate_payment_status(
        self, payment_id: str, status: str, *, error_code: str | int | None = None
    ) -> None:
        """`POST /simulate/payments/{id}` — advance a mocked payment's status. **Dev only** (Method
        rejects it elsewhere). This is what lets the hard gate drive `posted` and a forced
        `reversed` without waiting on a real settlement clock."""
        payload: dict[str, Any] = {"status": status}
        if error_code is not None:
            payload["error_code"] = error_code
        _data(self._http.post(f"/simulate/payments/{payment_id}", json=payload))

    def ping(self) -> int:
        """A cheap authenticated read (`GET /entities`) — a connectivity/auth check. Returns the
        HTTP status; raises `MethodApiError` (with the body) on 4xx/5xx so a bad key surfaces as a
        401 with Method's reason, not a silent empty list."""
        resp = self._http.get("/entities")
        if resp.is_error:
            raise MethodApiError(
                resp.status_code, resp.request.method, str(resp.request.url), resp.text
            )
        return resp.status_code

    # --- dev fixtures: build a payable entity → source → destination ----------------
    #
    # A payment needs a platform `source` and a card `destination`, and neither exists on a fresh
    # account. In **dev** (mocked) these can be built from the API: create an entity, attach an ACH
    # account as the source, and run Connect to *discover* the entity's liability accounts (a
    # liability cannot be created directly — "case-by-case, contact your CSM" — but Connect surfaces
    # the mocked ones dev seeds). Harmless in any env; only the smoke script/gate call them.

    def create_entity(self, *, first_name: str = "Test", last_name: str = "Payer") -> str:
        """`POST /entities` — a minimal individual. Returns the entity id (`ent_…`)."""
        data = _data(
            self._http.post(
                "/entities",
                json={
                    "type": "individual",
                    "individual": {"first_name": first_name, "last_name": last_name},
                },
            )
        )
        return data["id"]

    def create_ach_source(
        self, *, holder_id: str, routing: str, number: str, account_type: str = "checking"
    ) -> str:
        """`POST /accounts` with an `ach` block — a depository account to pay FROM. Returns
        `acc_…`."""
        data = _data(
            self._http.post(
                "/accounts",
                json={
                    "holder_id": holder_id,
                    "ach": {"routing": routing, "number": number, "type": account_type},
                },
            )
        )
        return data["id"]

    def connect_entity(self, entity_id: str) -> list[str]:
        """`POST /entities/{id}/connect` — discover the entity's liability accounts. Returns the
        `accounts` array (the `acc_…` ids Connect found), which is empty for an entity dev seeds no
        liabilities for."""
        data = _data(self._http.post(f"/entities/{entity_id}/connect", json={}))
        return list(data.get("accounts", []))

    def list_accounts(self) -> list[dict[str, Any]]:
        """`GET /accounts` — every account on the API key. Used to reuse a source/destination that
        already exists rather than minting a new one each run."""
        return list(_data(self._http.get("/accounts")))

    def get_account(self, account_id: str) -> dict[str, Any]:
        """`GET /accounts/{id}` — one account, to read its `type` (a payment destination must be a
        `liability`)."""
        return _data(self._http.get(f"/accounts/{account_id}"))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MethodHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _data(resp: httpx.Response) -> Any:
    """The `data` out of Method's `{success, data, message}` envelope — or the flat body if a
    response is not wrapped. Raises `MethodApiError` (with the body) on a non-2xx first, so an error
    body never reads as data and the vendor's explanation is never swallowed."""
    if resp.is_error:
        raise MethodApiError(
            resp.status_code, resp.request.method, str(resp.request.url), resp.text
        )
    body = resp.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _error_code(payment: dict[str, Any]) -> str | None:
    """The error code off a payment, whether Method reports it as a bare code or an `{error: {...}}`
    object. `None` when the payment carries no error."""
    error = payment.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return None if code is None else str(code)
    return None if error is None else str(error)
