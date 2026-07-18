"""Provider webhooks: verify the signature, reject replays, persist, enqueue — never work inline
(ticket 0042, U4).

Status and returns arrive by webhook (KTD-5), an inbound internet-facing signal that pushes a money
saga toward `settled` or unwinds `SWEEP_IN_FLIGHT`. A forged or replayed callback must not fake a
settlement, so the signature is verified and the timestamp freshness-checked *before* the payload
touches the ledger — exactly the treatment `backend/plaid/webhook.py` gives the Plaid doorbell. The
two vendors sign differently:

- **Increase** (Standard Webhooks): headers `webhook-id` / `webhook-timestamp` / `webhook-signature`
  (`v1,<base64 HMAC-SHA256>` over `id.timestamp.body`), ~5-minute freshness.
- **Method**: header `method-webhook-signature` = hex HMAC-SHA256 over `timestamp:body`, with
  `method-webhook-timestamp`.

Persistence is the unscoped `transfer_webhooks` store deduped on `(provider, event_id)` NULLS NOT
DISTINCT; the worker then resolves the household and advances the saga (`saga.apply_status`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Reject a signature whose timestamp is more than five minutes old — the replay window both vendors
# recommend closing.
_MAX_SKEW_SECONDS = 300


class TransferWebhookError(Exception):
    """The signature was absent, malformed, stale, or did not verify. Do not trust the payload."""


def _fresh(timestamp: str) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(time.time() - ts) <= _MAX_SKEW_SECONDS


def verify_increase(headers: dict[str, str], body: bytes, secret: str) -> None:
    """Verify an Increase Standard Webhooks signature over `id.timestamp.body`, or raise."""
    webhook_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signature = headers.get("webhook-signature")
    if not (webhook_id and timestamp and signature):
        raise TransferWebhookError("missing Increase webhook signature headers")
    if not _fresh(timestamp):
        raise TransferWebhookError("Increase webhook timestamp is stale or malformed")

    signed = f"{webhook_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode()
    # The header may carry several space-separated `v1,<sig>` versions; any match verifies.
    candidates = [part.split(",", 1)[1] for part in signature.split() if part.startswith("v1,")]
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise TransferWebhookError("Increase webhook signature did not verify")


def verify_method(headers: dict[str, str], body: bytes, secret: str) -> None:
    """Verify a Method webhook signature (hex HMAC-SHA256 over `timestamp:body`), or raise."""
    timestamp = headers.get("method-webhook-timestamp")
    signature = headers.get("method-webhook-signature")
    if not (timestamp and signature):
        raise TransferWebhookError("missing Method webhook signature headers")
    if not _fresh(timestamp):
        raise TransferWebhookError("Method webhook timestamp is stale or malformed")

    signed = f"{timestamp}:".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise TransferWebhookError("Method webhook signature did not verify")


def persist_webhook(
    engine: Engine,
    *,
    provider: str,
    provider_transfer_id: str | None,
    event_type: str,
    event_id: str | None,
    payload: dict[str, Any],
) -> bool:
    """Insert the raw webhook under the dedup key. Return True only if a new row was written.

    Unscoped on purpose (ADR-0006): a webhook names a provider ref, not a household, and RLS would
    fail closed. `ON CONFLICT DO NOTHING` on the NULLS-NOT-DISTINCT key drops a redelivery.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO transfer_webhooks"
                " (id, provider, provider_transfer_id, event_type, event_id, payload)"
                " VALUES (:id, :provider, :ref, :etype, :eid, CAST(:payload AS JSONB))"
                " ON CONFLICT ON CONSTRAINT uq_transfer_webhooks_dedup DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "provider": provider,
                "ref": provider_transfer_id,
                "etype": event_type,
                "eid": event_id,
                "payload": json.dumps(payload),
            },
        )
        return result.rowcount > 0
