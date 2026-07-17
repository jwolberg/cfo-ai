"""Handing sync work to Cloud Tasks — the doorbell never runs it inline.

`architecture.md` [3.1]: "a slow handler causes retries, which cause the duplicates you are trying
to avoid." So the doorbell persists the raw payload and enqueues here, returning at once. The push
target carries an **OIDC token**, so only Cloud Tasks (as the configured service account) can invoke
the worker route — the enqueue is authenticated end to end, not a public URL anyone can POST to.

**Nothing checks this queue exists at startup.** Unlike `assert_rls_binds`/`_assert_migrated`, a
missing queue or an ungranted service account fails at the *first webhook*, not at deploy. That is
why the config is read here and named loudly, and why ADR-0005 lists the provisioning steps: create
the queue, grant `cloudtasks.enqueuer`, and require the OIDC token on the push target.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from google.cloud import tasks_v2


class QueueNotConfigured(RuntimeError):
    """Raised when an enqueue is attempted without the queue configured. Never on a good call."""


_REQUIRED = (
    "PLAID_TASKS_PROJECT",
    "PLAID_TASKS_LOCATION",
    "PLAID_TASKS_QUEUE",
    "PLAID_SYNC_WORKER_URL",
    "PLAID_TASKS_SERVICE_ACCOUNT",
)


def _config() -> dict[str, str]:
    values = {name: os.environ.get(name) for name in _REQUIRED}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise QueueNotConfigured(
            f"the Cloud Tasks queue is not configured: {', '.join(missing)} unset. The webhook "
            "doorbell cannot hand off sync work. See docs/decisions/0005 for provisioning."
        )
    return values  # type: ignore[return-value]


def enqueue_sync(
    plaid_item_id: str,
    *,
    client: tasks_v2.CloudTasksClient | None = None,
) -> str:
    """Enqueue a `/transactions/sync` for one item, targeting the worker route with an OIDC token.

    Returns the created task name. `client` is injectable so tests never touch Cloud Tasks.
    """
    cfg = _config()
    client = client or tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        cfg["PLAID_TASKS_PROJECT"], cfg["PLAID_TASKS_LOCATION"], cfg["PLAID_TASKS_QUEUE"]
    )
    worker_url = cfg["PLAID_SYNC_WORKER_URL"]
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": worker_url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"plaid_item_id": plaid_item_id}).encode(),
            # Only Cloud Tasks, as this service account, can mint a token for this audience — so the
            # worker route can require it and reject anything else.
            "oidc_token": {
                "service_account_email": cfg["PLAID_TASKS_SERVICE_ACCOUNT"],
                "audience": worker_url,
            },
        }
    }
    return client.create_task(parent=parent, task=task).name


# The type a route depends on, so a test can substitute a recorder without a real queue.
Enqueuer = Callable[[str], str]
