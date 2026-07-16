"""The schema, scoped by household.

SQLAlchemy **Core**, not the ORM. ADR-0002 was proud of "no ORM" and ADR-0004 keeps the spirit
of it: the SQL stays visible, there is no session-identity map, and nothing lazy-loads a card
behind your back. Alembic exists because a schema without migrations drifts.

Every column here mirrors a type in `engine/models.py`. Where the engine says `Decimal | None`,
the column is `NUMERIC` and nullable — most sharply `cards.apr`, which is nullable because
`engine/decide.py` refuses to rank rather than guess a missing one (`decision-engine.md` §6.3).

**Three rules that are not negotiable** (ADR-0004 [2]):

1. **`NUMERIC`, never float.** ADR-0002 [2.2] exists because "a cent that round-trips through a
   float is no longer the cent the engine decided on." `NUMERIC` is exact natively — the one place
   the database is *stronger* than the artifact it replaces, and the reason the `$dec` tagging
   survives only for the JSONB snapshot payload.
2. **`household_id`, not `user_id`.** A `user` is a login; the household is the tenant. One
   household may eventually have two logins, and for this product that is not a footnote — a
   spouse's spending is precisely what breaks a forecast. See `architecture.md` [4].
3. **RLS on every household-scoped table, and FORCED.** A policy the table owner is exempt from is
   decoration. `architecture.md` [4]: "one forgotten `WHERE` clause is not an acceptable single
   point of failure."
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

metadata = MetaData()

# The RLS session variable every scoped query runs under. Set with `SET LOCAL` inside a
# transaction, never `SET` — see `backend/db/repository.py`, and ticket 0021's warning.
RLS_VAR = "app.household_id"

# Money is NUMERIC(14, 2). Fourteen digits is ~$999 billion, which is absurd for a household and
# is the point: the constraint should never be the thing that fails, and a narrower type buys
# nothing. Two decimal places because `money()` quantizes to the cent and anything else is a lie
# about precision we do not have.
MONEY = Numeric(14, 2)

# APR is a rate, not money: NUMERIC(6, 5) holds 0.23990 exactly. `engine/models.py` validates
# 0 <= apr <= 2; the CHECK below is the same claim, made where the data lives.
RATE = Numeric(6, 5)


households = Table(
    "households",
    metadata,
    Column("id", Text, primary_key=True),
    # Which archetype seeded this household (`backend/archetypes.py`, ticket 0023). Null for a
    # real household, which is the point of recording it: synthetic and real must be tellable
    # apart in any number either one appears in.
    Column("archetype", Text, nullable=True),
    # Crypto-shredding (ADR-0004 [2], C4). One data-encryption key per household in KMS; a CCPA
    # erasure destroys the key, the rows stay, and the bytes become unrecoverable — so the
    # append-only decision log holds and erasure does not punch holes in the population
    # `prd.md` §5.2 says must be measured population-wide.
    #
    # The column lands now because it is the expensive half to retrofit. There is no key behind
    # it yet: synthetic households have no PII, and the KMS wiring lands with Plaid.
    Column("dek_id", Text, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    # Set when the key is destroyed. Not a soft delete — the data is genuinely unrecoverable at
    # that point; this records *when*, for the audit trail that outlives the plaintext.
    Column("deleted_at", TIMESTAMP(timezone=True), nullable=True),
)

accounts = Table(
    "accounts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("household_id", Text, ForeignKey("households.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),  # engine.models.AccountKind
    Column("balance", MONEY, nullable=False),
    Column("connection", Text, nullable=False),  # engine.models.ConnectionState
    Column("balance_age_days", Integer, nullable=False),
    CheckConstraint("balance_age_days >= 0", name="ck_accounts_balance_age_nonneg"),
)

cards = Table(
    "cards",
    metadata,
    Column("id", Text, primary_key=True),
    Column("household_id", Text, ForeignKey("households.id", ondelete="CASCADE"), nullable=False),
    # Nullable, and load-bearing. Plaid's Liabilities product does not return APR for many
    # issuers (`decision-engine.md` §6.3). `_select_target` refuses with APR_UNKNOWN rather than
    # guess a target card — "a wrong target card looks exactly like working while quietly
    # destroying the entire value proposition."
    Column("apr", RATE, nullable=True),
    Column("close_day_of_month", Integer, nullable=False),  # engine.models.StatementCycle
    Column("grace_days", Integer, nullable=False),
    Column("statement_balance", MONEY, nullable=False),
    Column("statement_due_date", Date, nullable=False),
    Column("minimum_payment", MONEY, nullable=False),
    Column("unbilled_balance", MONEY, nullable=False),
    Column("next_close_date", Date, nullable=False),
    Column("behavior", Text, nullable=False),  # engine.models.PaymentBehavior
    Column("observed_monthly_payment", MONEY, nullable=True),
    Column("observed_monthly_charges", MONEY, nullable=True),
    CheckConstraint("apr IS NULL OR (apr >= 0 AND apr <= 2)", name="ck_cards_apr_range"),
    CheckConstraint(
        "close_day_of_month BETWEEN 1 AND 28", name="ck_cards_close_day"
    ),  # 28: every month has one
)

policies = Table(
    "policies",
    metadata,
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("buffer_floor", MONEY, nullable=False),
    Column("max_sweep", MONEY, nullable=False),
    Column("max_weekly_sweep", MONEY, nullable=False),
    # `decision-engine.md` §9. Ships at 7. `0` restores the daily engine exactly, which is why it
    # is a policy value and not a constant.
    Column("min_days_between_sweeps", Integer, nullable=False, server_default=text("7")),
    Column("blackout_dates", ARRAY(Date), nullable=False, server_default=text("'{}'")),
    CheckConstraint("min_days_between_sweeps >= 0", name="ck_policies_spacing_nonneg"),
)

# Partitioned by month on `day` from the first migration. Free at zero rows; a migration
# scheduled around a 365M-row table later (ADR-0004 [2]). The partitions themselves are created
# in the migration, not here — SQLAlchemy Core has no declarative partition syntax, and inventing
# one would hide the DDL this schema exists to keep visible.
decisions = Table(
    "decisions",
    metadata,
    Column("id", Text, nullable=False),
    Column("household_id", Text, nullable=False),
    Column("day", Date, nullable=False),
    Column("action", Text, nullable=False),  # engine.models.Action
    Column("amount", MONEY, nullable=False),
    Column("target_card_id", Text, nullable=True),
    # None on a blocking refusal — it never ran a forecast. `outcome.grade()` *raises* on those
    # rather than scoring a zero that would look like a perfect forecast (`decision-engine.md`
    # §8, §9.1). The column is nullable for exactly that reason: it is a real absence, not a gap.
    Column("projected_low_balance", MONEY, nullable=True),
    # Queryable on purpose — refusal-rate metrics slice it. Unlike the snapshot payload, which is
    # opaque behind `SnapshotStore`.
    Column("reasons", JSONB, nullable=False),
    Column("engine_version", Text, nullable=False),
    # Opaque. "pg:<id>" today, "gs://..." if the seam ever fires. Nothing parses it — ticket 0022.
    Column("snapshot_ref", Text, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    # The partition key must be in the primary key. (household_id, day) is also the sort order
    # that makes snapshots compress ~70x if they ever move to columnar storage (ADR-0004 [2]).
    CheckConstraint("amount >= 0", name="ck_decisions_amount_nonneg"),
    CheckConstraint(
        "(action = 'sweep' AND amount > 0) OR (action = 'refuse' AND amount = 0)",
        name="ck_decisions_amount_matches_action",
    ),
    postgresql_partition_by="RANGE (day)",
)

# Reached **only** through `backend/db/snapshots.py`'s `SnapshotStore`. The payload is the frozen
# `Snapshot` the engine actually saw, encoded with `artifact.py`'s tagged-scalar codec — because
# it is JSON and JSON still has no decimal type. `architecture.md` [3.3]: "Store the inputs, not
# references to the inputs. The difference between an audit trail and a story."
snapshots = Table(
    "snapshots",
    metadata,
    Column("id", Text, primary_key=True),
    Column("household_id", Text, ForeignKey("households.id", ondelete="CASCADE"), nullable=False),
    Column("day", Date, nullable=False),
    Column("payload", JSONB, nullable=False),
)


# Every table whose rows belong to exactly one household. RLS goes on each, the repository scopes
# each, and ticket 0021's IDOR suite proves both — independently.
HOUSEHOLD_SCOPED: tuple[str, ...] = (
    "accounts",
    "cards",
    "policies",
    "decisions",
    "snapshots",
)

__all__ = [
    "HOUSEHOLD_SCOPED",
    "MONEY",
    "RATE",
    "RLS_VAR",
    "accounts",
    "cards",
    "decisions",
    "households",
    "metadata",
    "policies",
    "snapshots",
]
