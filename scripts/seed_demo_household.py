"""Import a linked Plaid-Sandbox household into a target DB as a READ-ONLY demo household.

Ticket 0057, the data half of the read-only demo-plane. A household linked locally against Plaid
Sandbox becomes an `is_demo` household the public demo viewer can *read* (never write): its Plaid
rows are copied verbatim, a policy + a matching attestation are seeded so `/live-decision` and
`/spend` produce a real decision, and the demo Stytch user (whose id the `/demo/session` token
carries) is added as a **viewer**. Nothing here calls Plaid — the rows are copied, so the target
API needs no Plaid credentials.

    SOURCE_DATABASE_URL=<where the linked household lives>   # defaults to DATABASE_URL (same DB)
    DATABASE_URL=<target: local test, then Neon>
    SOURCE_HOUSEHOLD_ID=hh_...            # the linked household to copy
    DEMO_HOUSEHOLD_ID=hh_demo_plaid       # the demo household to (re)create  [default]
    STYTCH_PROJECT_ID=... STYTCH_SECRET=...
    DEMO_STYTCH_EMAIL=... DEMO_STYTCH_PASSWORD=...
      .venv/bin/python scripts/seed_demo_household.py

Idempotent: it deletes and recreates DEMO_HOUSEHOLD_ID (a cascade drops its old rows) each run.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

from sqlalchemy import create_engine, text

from backend.attestation import card_fingerprint, current_card_ids
from backend.db.repository import add_user, get_user_by_stytch_id, repository


def _demo_stytch_user_id() -> tuple[str, str]:
    """The demo Stytch user's id (the `sub` its `/demo/session` tokens carry) and email — the
    identity the demo viewer row reconciles onto. Create-or-authenticate, as the endpoint does."""
    from stytch import Client

    project_id = os.environ["STYTCH_PROJECT_ID"]
    email = os.environ["DEMO_STYTCH_EMAIL"]
    password = os.environ["DEMO_STYTCH_PASSWORD"]
    client = Client(
        project_id=project_id,
        secret=os.environ["STYTCH_SECRET"],
        environment="test" if project_id.startswith("project-test-") else "live",
        suppress_warnings=True,
    )
    try:
        resp = client.passwords.authenticate(
            email=email, password=password, session_duration_minutes=5
        )
    except Exception:  # noqa: BLE001 — first run: create the demo user
        resp = client.passwords.create(email=email, password=password, session_duration_minutes=5)
    return resp.user_id, email


def _read_source(source_url: str, source_hh: str) -> dict:
    """Every Plaid row the demo household needs, read from the source DB under household scope."""
    engine = create_engine(source_url)
    with repository(engine, source_hh) as repo:
        items = (
            repo.conn.execute(
                text(
                    "SELECT plaid_item_id, access_token, institution_id FROM plaid_items"
                    " WHERE household_id = :h"
                ),
                {"h": source_hh},
            )
            .mappings()
            .all()
        )
        return {
            "items": [dict(r) for r in items],
            "accounts": [dict(a) for a in repo.latest_plaid_accounts()],
            "liabilities": [dict(a) for a in repo.latest_plaid_liabilities()],
            "txns": [dict(t) for t in repo.plaid_transactions()],
        }


def main() -> None:
    source_url = os.environ.get("SOURCE_DATABASE_URL", os.environ["DATABASE_URL"])
    target_url = os.environ["DATABASE_URL"]
    source_hh = os.environ["SOURCE_HOUSEHOLD_ID"]
    demo_hh = os.environ.get("DEMO_HOUSEHOLD_ID", "hh_demo_plaid")

    stytch_user_id, email = _demo_stytch_user_id()
    data = _read_source(source_url, source_hh)
    if not data["accounts"]:
        raise SystemExit(f"source household {source_hh} has no plaid_accounts — nothing to import")

    target = create_engine(target_url)

    # The demo user (reconciled onto the real Stytch id) and a clean demo household. `households`
    # has no RLS; the DELETE cascades to the household's members/plaid/policy/attestation, so a
    # re-run converges rather than accumulating.
    with target.connect() as c, c.begin():
        add_user(c, user_id=f"user_{uuid.uuid4().hex}", stytch_user_id=stytch_user_id, email=email)
        demo_user_id = get_user_by_stytch_id(c, stytch_user_id)["id"]
        c.execute(text("DELETE FROM households WHERE id = :h"), {"h": demo_hh})
        c.execute(
            text("INSERT INTO households (id, archetype, is_demo) VALUES (:h, NULL, true)"),
            {"h": demo_hh},
        )

    # Plaid ids (item, account, transaction) are globally unique, and demo data should not carry
    # the real item id or a live access token anyway — the demo never syncs. So remap every Plaid id
    # to a fresh synthetic one, consistently, and use a placeholder token. The relationships that
    # `build_linked_history` reads are by `plaid_account_id`, so a consistent remap preserves them.
    new_item_id = f"demo-item-{uuid.uuid4().hex[:16]}"
    acct_map = {
        a["plaid_account_id"]: f"demo-acct-{uuid.uuid4().hex[:16]}" for a in data["accounts"]
    }

    with repository(target, demo_hh) as repo:
        repo.add_plaid_item(
            item_id=f"pi_{uuid.uuid4().hex}",
            plaid_item_id=new_item_id,
            access_token="demo-no-sync",
            institution_id=(data["items"][0].get("institution_id") if data["items"] else None),
        )
        for a in data["accounts"]:
            repo.add_plaid_account(
                plaid_item_id=new_item_id,
                plaid_account_id=acct_map[a["plaid_account_id"]],
                name=a.get("name"),
                official_name=a.get("official_name"),
                type=a.get("type"),
                subtype=a.get("subtype"),
                current_balance=a.get("current_balance"),
                available_balance=a.get("available_balance"),
                iso_currency_code=a.get("iso_currency_code"),
            )
        for liab in data["liabilities"]:
            mapped = acct_map.get(liab["plaid_account_id"])
            if mapped is None:
                continue  # a liability for an account we didn't copy — skip rather than orphan it
            repo.add_plaid_liability(
                plaid_item_id=new_item_id,
                plaid_account_id=mapped,
                last_statement_balance=liab.get("last_statement_balance"),
                last_statement_issue_date=liab.get("last_statement_issue_date"),
                minimum_payment=liab.get("minimum_payment"),
                next_payment_due_date=liab.get("next_payment_due_date"),
                purchase_apr=liab.get("purchase_apr"),
                is_overdue=liab.get("is_overdue"),
            )
        for t in data["txns"]:
            mapped = acct_map.get(t["plaid_account_id"])
            if mapped is None:
                continue
            repo.add_plaid_transaction(
                transaction_id=f"pt_{uuid.uuid4().hex}",
                plaid_item_id=new_item_id,
                plaid_account_id=mapped,
                plaid_transaction_id=f"demo-txn-{uuid.uuid4().hex[:20]}",
                change_type="added",
                amount=t.get("amount"),
                date=t.get("date"),
                name=t.get("name"),
                merchant_name=t.get("merchant_name"),
            )
        # A sensible policy so the household is decidable (not NoLivePolicy), and an attestation
        # over the very cards we just copied: `current_card_ids` reads them back, so the fingerprint
        # matches what the live path checks and coverage is COMPLETE.
        repo.set_policy(
            buffer_floor=Decimal("500.00"),
            max_sweep=Decimal("200.00"),
            max_weekly_sweep=Decimal("400.00"),
            min_days_between_sweeps=7,
            blackout_dates=[],
        )
        repo.add_attestation(card_fingerprint=card_fingerprint(current_card_ids(repo)))
        repo.add_membership(user_id=demo_user_id, role="viewer")

    print(
        f"imported {demo_hh}: {len(data['accounts'])} accounts, {len(data['liabilities'])} "
        f"liabilities, {len(data['txns'])} txns; demo viewer {demo_user_id} ({stytch_user_id})"
    )


if __name__ == "__main__":
    main()
