"""MethodHttpClient request/response shaping — no network, no key needed (ticket 0044).

An `httpx.MockTransport` stands in for Method, so these prove the client sends the right request and
parses Method's real response shape, without a live key or the sandbox. The end-to-end proof against
the actual API is the smoke script (`scripts/method_smoke.py`) and the hard gate
(`tests/test_transfer_sandbox.py`); this pins the wire contract underneath both.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.transfer.method_client import MethodConfigError, MethodHttpClient


def _client(handler) -> MethodHttpClient:
    return MethodHttpClient("sk_test_key", env="dev", transport=httpx.MockTransport(handler))


def test_unknown_environment_is_refused() -> None:
    with pytest.raises(MethodConfigError):
        MethodHttpClient("sk_test_key", env="staging")


def test_create_payment_posts_the_right_shape_and_returns_the_id() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"success": True, "data": {"id": "pmt_abc", "status": "pending"}}
        )

    with _client(handler) as client:
        pid = client.create_payment(
            source_id="acc_src",
            destination_id="acc_dst",
            amount_cents=100,
            description="RESFI",
            idempotency_key="key-1",
        )

    assert pid == "pmt_abc"
    assert seen["url"] == "https://dev.methodfi.com/payments"
    assert seen["auth"] == "Bearer sk_test_key"
    assert seen["idem"] == "key-1"  # sent even though Method's docs omit it (KTD-3 reconciliation)
    assert seen["body"] == {
        "amount": 100,
        "source": "acc_src",
        "destination": "acc_dst",
        "description": "RESFI",
    }


def test_dry_run_sets_the_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["dry_run"] is True
        return httpx.Response(200, json={"data": {"id": "pmt_dry", "status": "pending"}})

    with _client(handler) as client:
        assert (
            client.create_payment(
                source_id="s",
                destination_id="d",
                amount_cents=100,
                description="RESFI",
                idempotency_key="k",
                dry_run=True,
            )
            == "pmt_dry"
        )


def test_get_payment_reads_status_and_error_from_an_error_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/payments/pmt_x"
        return httpx.Response(
            200, json={"data": {"id": "pmt_x", "status": "failed", "error": {"code": "10001"}}}
        )

    with _client(handler) as client:
        view = client.get_payment("pmt_x")
    assert view.status == "failed"
    assert view.error_code == "10001"


def test_get_payment_handles_a_flat_body_and_no_error() -> None:
    # A response that is not wrapped in {data: ...}, and a payment with no error field.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "pmt_y", "status": "posted"})

    with _client(handler) as client:
        view = client.get_payment("pmt_y")
    assert view.status == "posted"
    assert view.error_code is None


def test_simulate_posts_status_to_the_dev_endpoint() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "pmt_z", "status": "posted"}})

    with _client(handler) as client:
        client.simulate_payment_status("pmt_z", "posted")

    assert seen["path"] == "/simulate/payments/pmt_z"
    assert seen["body"] == {"status": "posted"}


def test_a_4xx_raises_rather_than_returning_a_bad_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"success": False, "message": "unauthorized"})

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.ping()
