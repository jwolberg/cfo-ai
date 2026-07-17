"""FastAPI dependencies shared by the Plaid routes.

Defined once, in one module, so a test that overrides `get_plaid_client` or `get_enqueuer` overrides
it for *every* route — the doorbell and the exchange both resolve the same dependency object. The
alternative, a copy per router, is an override that silently misses half the surface.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.engine import Engine

from backend.plaid.client import plaid_client
from backend.plaid.tasks import Enqueuer, enqueue_sync


def get_engine(request: Request) -> Engine:
    return request.app.state.db


def get_plaid_client():
    """The lazily-built Plaid client. Tests override this with a fake (tests/test_webhook.py)."""
    return plaid_client()


def get_enqueuer() -> Enqueuer:
    """The Cloud Tasks enqueuer. Tests override this with a recorder so no queue is touched."""
    return enqueue_sync
