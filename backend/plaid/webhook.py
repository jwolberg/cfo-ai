"""The webhook doorbell: verify, persist, enqueue — never work inline.

`architecture.md` [3.1]: the handler "persists the raw payload and enqueues, full stop ... a slow
handler causes retries, which cause the duplicates you are trying to avoid." So there is no Plaid
call and no sync in this request path. Three things happen and nothing else:

1. **Verify the `Plaid-Verification` JWT.** The endpoint is public — it does not carry our API key,
   because Plaid does not have it. Its authentication is the signature: an ES256 JWT whose `kid`
   names a key fetched from `/webhook_verification_key/get`, whose signature must verify, and whose
   `request_body_sha256` claim must match *this* body. An unsigned or forged payload is rejected
   before it touches the table or the queue.
2. **Persist the raw payload**, unscoped, under the dedup key (`plaid_webhooks`, ADR-0005).
3. **Enqueue a sync** — but only when the row was newly inserted. A redelivered webhook conflicts on
   the dedup key, inserts nothing, and enqueues nothing; the sync is idempotent by cursor anyway.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Header, Request, Response, status
from jwt.algorithms import ECAlgorithm
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.plaid.deps import get_engine, get_enqueuer, get_plaid_client
from backend.plaid.tasks import Enqueuer

router = APIRouter(prefix="/plaid", tags=["plaid"])

# Plaid recommends rejecting a verification JWT whose `iat` is more than 5 minutes old.
_MAX_AGE = dt.timedelta(minutes=5)

# kid -> the EC public key parsed from Plaid's JWK. Plaid's keys are stable, so cache them; a key
# rotation is a new kid and a cache miss, which fetches afresh.
_key_cache: dict[str, object] = {}


class WebhookVerificationError(Exception):
    """The `Plaid-Verification` JWT was absent, malformed, unsigned, stale, or signed a different
    body. Every one of them means: do not trust this payload."""


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _public_key_for(kid: str, client) -> object:
    if kid not in _key_cache:
        from plaid.model.webhook_verification_key_get_request import (
            WebhookVerificationKeyGetRequest,
        )

        response = client.webhook_verification_key_get(WebhookVerificationKeyGetRequest(key_id=kid))
        # Plaid's webhook keys are ES256 over P-256; the JWK comes back as a model, `.to_dict()` it.
        _key_cache[kid] = ECAlgorithm.from_jwk(json.dumps(response.key.to_dict()))
    return _key_cache[kid]


def verify_webhook(token: str, body: bytes, client) -> dict:
    """Verify the JWT signs *this* body with a key Plaid vouches for. Return claims or raise.

    The body-hash check is what stops a replayed-but-valid signature being pointed at a different
    payload: the JWT commits to `request_body_sha256`, and we recompute it over the exact bytes.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise WebhookVerificationError("the Plaid-Verification header is not a JWT") from exc

    if header.get("alg") != "ES256":
        raise WebhookVerificationError(
            f"unexpected JWT alg {header.get('alg')!r}; Plaid signs webhooks with ES256"
        )
    kid = header.get("kid")
    if not kid:
        raise WebhookVerificationError("the JWT header carries no kid")

    key = _public_key_for(kid, client)
    try:
        claims = jwt.decode(token, key=key, algorithms=["ES256"], options={"require": ["iat"]})
    except jwt.PyJWTError as exc:
        raise WebhookVerificationError("the JWT signature did not verify") from exc

    issued = dt.datetime.fromtimestamp(claims["iat"], tz=dt.timezone.utc)
    if _now() - issued > _MAX_AGE:
        raise WebhookVerificationError("the JWT is stale (iat older than 5 minutes)")

    expected = claims.get("request_body_sha256")
    actual = hashlib.sha256(body).hexdigest()
    if not expected or not hmac.compare_digest(str(expected), actual):
        raise WebhookVerificationError("the request body hash does not match the signed claim")

    return claims


def _persist(
    engine: Engine, item_id: str, wtype: str, wcode: str, cursor: str | None, payload: dict
) -> bool:
    """Insert the raw webhook under the dedup key. Return True only if a new row was written.

    Unscoped on purpose (ADR-0005): a webhook names an item, not a household, and RLS would fail
    closed here. `ON CONFLICT DO NOTHING` on the NULLS-NOT-DISTINCT dedup key drops a redelivery.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO plaid_webhooks"
                " (id, plaid_item_id, webhook_type, webhook_code, cursor, payload)"
                " VALUES (:id, :item, :wtype, :wcode, :cursor, CAST(:payload AS JSONB))"
                " ON CONFLICT ON CONSTRAINT uq_plaid_webhooks_dedup DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "item": item_id,
                "wtype": wtype,
                "wcode": wcode,
                "cursor": cursor,
                "payload": json.dumps(payload),
            },
        )
        return result.rowcount > 0


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def plaid_webhook(
    request: Request,
    engine: Annotated[Engine, Depends(get_engine)],
    client: Annotated[Any, Depends(get_plaid_client)],
    enqueue: Annotated[Enqueuer, Depends(get_enqueuer)],
    plaid_verification: Annotated[str | None, Header(alias="Plaid-Verification")] = None,
) -> Response:
    body = await request.body()

    if not plaid_verification:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        verify_webhook(plaid_verification, body, client)
    except WebhookVerificationError:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # The hash matched, so these bytes are exactly what Plaid signed — safe to parse.
    payload = json.loads(body)
    item_id = payload.get("item_id")
    webhook_type = payload.get("webhook_type")
    webhook_code = payload.get("webhook_code")
    if not (item_id and webhook_type and webhook_code):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    inserted = _persist(engine, item_id, webhook_type, webhook_code, payload.get("cursor"), payload)
    if inserted:
        enqueue(item_id)
    return Response(status_code=status.HTTP_200_OK)
