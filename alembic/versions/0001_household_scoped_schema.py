"""The first migration: households, RLS, and a partitioned decision log.

Revision ID: 0001
Revises:
Create Date: 2026-07-16

Ticket 0020. See ADR-0004 and `architecture.md` [4].

Three things here are not SQLAlchemy-expressible and are written as raw DDL on purpose — the
alternative is a helper that hides the exact statements a reviewer most needs to read:

1. **`PARTITION BY RANGE (day)`** on `decisions`, plus its partitions.
2. **`FORCE ROW LEVEL SECURITY`** on every household-scoped table.
3. **The application role**, which must not own the tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.db.models import HOUSEHOLD_SCOPED, RLS_VAR
from backend.db.session import APP_ROLE

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# The window the demo seeds (`backend/precompute.py`: WINDOW_START, WARMUP_DAYS + SERVED_DAYS),
# with room either side. Monthly, from the first migration: free at zero rows, a migration
# scheduled around a 365M-row table later.
#
# The DEFAULT partition is not a convenience — it is the difference between "a day outside the
# range is rejected" and "a day outside the range is silently lost". A decision that cannot be
# written must fail loudly; `pg_partman` or a scheduled job replaces this before it matters.
PARTITION_MONTHS = [(y, m) for y in (2025, 2026, 2027) for m in range(1, 13)]


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("archetype", sa.Text, nullable=True),
        sa.Column("dek_id", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("connection", sa.Text, nullable=False),
        sa.Column("balance_age_days", sa.Integer, nullable=False),
        sa.CheckConstraint("balance_age_days >= 0", name="ck_accounts_balance_age_nonneg"),
    )
    op.create_index("ix_accounts_household", "accounts", ["household_id"])

    op.create_table(
        "cards",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("apr", sa.Numeric(6, 5), nullable=True),
        sa.Column("close_day_of_month", sa.Integer, nullable=False),
        sa.Column("grace_days", sa.Integer, nullable=False),
        sa.Column("statement_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("statement_due_date", sa.Date, nullable=False),
        sa.Column("minimum_payment", sa.Numeric(14, 2), nullable=False),
        sa.Column("unbilled_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("next_close_date", sa.Date, nullable=False),
        sa.Column("behavior", sa.Text, nullable=False),
        sa.Column("observed_monthly_payment", sa.Numeric(14, 2), nullable=True),
        sa.Column("observed_monthly_charges", sa.Numeric(14, 2), nullable=True),
        sa.CheckConstraint("apr IS NULL OR (apr >= 0 AND apr <= 2)", name="ck_cards_apr_range"),
        sa.CheckConstraint("close_day_of_month BETWEEN 1 AND 28", name="ck_cards_close_day"),
    )
    op.create_index("ix_cards_household", "cards", ["household_id"])

    op.create_table(
        "policies",
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("buffer_floor", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_sweep", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_weekly_sweep", sa.Numeric(14, 2), nullable=False),
        sa.Column("min_days_between_sweeps", sa.Integer, nullable=False, server_default="7"),
        sa.Column(
            "blackout_dates",
            sa.ARRAY(sa.Date),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.CheckConstraint("min_days_between_sweeps >= 0", name="ck_policies_spacing_nonneg"),
    )

    op.create_table(
        "snapshots",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "household_id",
            sa.Text,
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_snapshots_household_day", "snapshots", ["household_id", "day"])

    # --- the partitioned decision log ------------------------------------------------
    #
    # No FK to households: Postgres does not support foreign keys FROM a partitioned table to
    # another table on older majors and it constrains partition maintenance on all of them. The
    # scoping is enforced by RLS and by the repository, which is where `architecture.md` [4] puts
    # it anyway ("at the repository layer AND by Postgres row-level security").
    op.execute(
        """
        CREATE TABLE decisions (
            id                    text        NOT NULL,
            household_id          text        NOT NULL,
            day                   date        NOT NULL,
            action                text        NOT NULL,
            amount                numeric(14,2) NOT NULL,
            target_card_id        text,
            projected_low_balance numeric(14,2),
            reasons               jsonb       NOT NULL,
            engine_version        text        NOT NULL,
            snapshot_ref          text,
            created_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_decisions_amount_nonneg CHECK (amount >= 0),
            CONSTRAINT ck_decisions_amount_matches_action CHECK (
                (action = 'sweep'  AND amount >  0) OR
                (action = 'refuse' AND amount =  0)
            ),
            -- The partition key must be in the primary key. (household_id, day) is also the sort
            -- order that makes snapshots compress ~70x if the SnapshotStore seam ever fires.
            PRIMARY KEY (household_id, day, id)
        ) PARTITION BY RANGE (day);
        """
    )

    for year, month in PARTITION_MONTHS:
        ny, nm = _next_month(year, month)
        op.execute(
            f"CREATE TABLE decisions_{year}_{month:02d} PARTITION OF decisions "
            f"FOR VALUES FROM ('{year}-{month:02d}-01') TO ('{ny}-{nm:02d}-01');"
        )

    # A day outside every range must fail loudly, not vanish. See PARTITION_MONTHS.
    op.execute("CREATE TABLE decisions_default PARTITION OF decisions DEFAULT;")

    # --- the application role --------------------------------------------------------
    #
    # It must not own the tables: RLS does not apply to the owner unless FORCEd, and relying on
    # FORCE alone means one `ALTER TABLE ... NO FORCE` is a silent, total authorization bypass.
    # Two independent reasons the policy binds, because ticket 0021's IDOR suite is only
    # meaningful if it cannot be undone by accident.
    #
    # ⚠️ **A role is cluster-wide; this migration is database-scoped.** That mismatch is real and
    # is why `downgrade()` does not drop it. Creating it here is a convenience for dev and CI —
    # idempotent, so a second database in the same cluster is a no-op. Provisioning it properly
    # belongs to infrastructure (ticket 0026), not to a schema migration.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE};")

    # Per-table, **not** `ON ALL TABLES IN SCHEMA public`. Two reasons, one of which bit:
    #
    # 1. `alembic_version` is Alembic's own bookkeeping and the application has no business
    #    touching it. `ALL TABLES` grants it anyway.
    # 2. That grant then makes `DROP ROLE` fail in `downgrade()` — "privileges for table
    #    alembic_version" — so the downgrade aborts halfway and the schema is left standing. A
    #    migration that cannot be reversed is not a migration, it is a one-way door with a
    #    `downgrade()` shaped decoration on it.
    for table in ("households", *HOUSEHOLD_SCOPED):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE};")

    # --- row-level security ----------------------------------------------------------
    #
    # USING governs what is visible to SELECT/UPDATE/DELETE; WITH CHECK governs what may be
    # written. Both, or a household scoped to A could INSERT a row owned by B — which is the same
    # breach in the other direction and is the one people forget.
    #
    # `current_setting(RLS_VAR, true)` — the `true` makes it return NULL instead of raising when
    # unset. NULL never equals household_id, so an unscoped query sees zero rows: RLS fails
    # closed, matching `architecture.md` [1.1]'s second principle.
    for table in HOUSEHOLD_SCOPED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {table}_household_isolation ON {table}
                USING      (household_id = current_setting('{RLS_VAR}', true))
                WITH CHECK (household_id = current_setting('{RLS_VAR}', true));
            """
        )


def downgrade() -> None:
    for table in HOUSEHOLD_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS {table}_household_isolation ON {table};")

    op.execute("DROP TABLE IF EXISTS decisions CASCADE;")  # takes its partitions with it
    op.drop_table("snapshots")
    op.drop_table("policies")
    op.drop_table("cards")
    op.drop_table("accounts")
    op.drop_table("households")

    # Dropping the tables above took their grants with them. `DROP OWNED BY` clears anything
    # left in **this** database — default ACLs, schema privileges — which is all a
    # database-scoped migration can honestly claim to clean up.
    op.execute(f"DROP OWNED BY {APP_ROLE};")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE};")

    # ⚠️ **The role is deliberately not dropped.**
    #
    # `DROP ROLE` is cluster-wide and this migration is database-scoped. If any other database in
    # the same cluster has ever run this migration — dev alongside test, or two Neon branches —
    # the role still holds grants there, and `DROP ROLE` fails with "role cfo_app cannot be
    # dropped because some objects depend on it: N objects in database X". The downgrade then
    # aborts *after* dropping the tables and leaves the schema half-reversed.
    #
    # That is not hypothetical: it is exactly what happened the first time this ran, and the
    # error named a database the migration had never heard of. A role is provisioning, not
    # schema — see `upgrade()`, and ticket 0026.
