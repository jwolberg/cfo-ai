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
    # Keyed `(household_id, id)`, not `id` — an account id is unique *within* a household. It was
    # global until migration `0003`, which was only ever survivable because one household existed:
    # every household the walk derives carries the same `chk_demo`/`sav_demo`, so the second one
    # collided. Every read is household-scoped twice over (repository + RLS), so nothing resolves an
    # id without a household to resolve it in.
    Column("id", Text, primary_key=True),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column("kind", Text, nullable=False),  # engine.models.AccountKind
    Column("balance", MONEY, nullable=False),
    Column("connection", Text, nullable=False),  # engine.models.ConnectionState
    Column("balance_age_days", Integer, nullable=False),
    CheckConstraint("balance_age_days >= 0", name="ck_accounts_balance_age_nonneg"),
)

cards = Table(
    "cards",
    metadata,
    # `(household_id, id)` — see `accounts` above, and migration `0003`. The 60-household population
    # `prd.md` §5.2 rests on is 60 `DEMO_SPEC` clones, every one of them holding `card_demo`: under
    # a global key that population is not merely unseeded, it is unseedable.
    Column("id", Text, primary_key=True),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
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
    # Where `apr` came from — engine.models.AprSource. Ticket 0028 settles architecture.md
    # §7.5 on "estimated, with visibly reduced confidence", and the confidence has to be
    # *stored* or it is not reduced, it is merely absent: a 23% estimate and a reported 23%
    # are the same number, and only this column tells them apart. `interest.py` refuses to
    # price an ESTIMATED rate, so losing this would silently start billing the KPI against a
    # guess.
    Column("apr_source", Text, nullable=False, server_default=text("'reported'")),
    CheckConstraint("apr IS NULL OR (apr >= 0 AND apr <= 2)", name="ck_cards_apr_range"),
    CheckConstraint(
        "apr_source IN ('reported', 'user_entered', 'estimated')", name="ck_cards_apr_source"
    ),
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

# **The one table here that ingest deletes.** Ticket 0031.
#
# It holds the half of the spend surface that only the full transaction `History` can answer — the
# rolling 30-day series by channel, and what each card took last cycle against what came off it.
# There is no `transactions` table (`architecture.md` [3.1], ingest, not built), so the seeder
# derives this from the history it walked and writes it once.
#
# **It is deliberately the smallest thing that cannot be derived.** The other half of the surface —
# each card's statement, its unbilled balance, and the reserve held against it — is *not* here, and
# must not be: `backend/spend.py` derives it from the stored `Snapshot` on every request. Storing it
# would be a second copy of what the snapshot already holds, free to disagree about what the engine
# saw, which is the argument `backend/readpath.py` makes against denormalizing display fields onto
# `decisions` and which [4.1] is a whole section about.
#
# JSONB rather than typed columns, unlike every other table in this file: the payload is one shape
# with one consumer, and it is **temporary**. Designing columns for data whose whole purpose is to
# be deleted by the next feature is work that would be thrown away with it. `snapshots` sets the
# precedent, and the tagged codec keeps the Decimals exact.
spend_projections = Table(
    "spend_projections",
    metadata,
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # The day the projection describes. `backend/spend.py:assemble` refuses to serve it beside a
    # snapshot from a different day rather than render two months side by side unlabelled.
    Column("as_of", Date, nullable=False),
    Column("payload", JSONB, nullable=False),
)


# One row per linked Plaid Item — a bank connection. Ticket 0034, the Plaid transport rung
# (`docs/plans/2026-07-17-001-feat-plaid-transport-rung-plan.md`). HOUSEHOLD_SCOPED and RLS-FORCEd
# like every table that holds a credential or a balance: the `access_token` *is* a credential.
#
# It is plaintext here because in Sandbox a token grants access to fabricated data and protects
# nothing, so KMS envelope encryption defers to the first real token (`architecture.md` [7.2]).
# The trigger fires quietly, though — the day `PLAID_ENV` flips to production the same column
# becomes a live credential — so `backend/db/session.py`'s start guard refuses a non-sandbox boot
# until the token is actually encrypted. `dek_id` (on `households`) is the other half of that story
# and still has no key behind it.
plaid_items = Table(
    "plaid_items",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Plaid's own stable id for the Item, unique across the whole deployment — it is Plaid's key,
    # not ours, and the webhook doorbell resolves `plaid_item_id → household_id` through it before
    # any household is known (ADR-0005). The named UNIQUE constraint lives in the migration.
    Column("plaid_item_id", Text, nullable=False, unique=True),
    Column("institution_id", Text, nullable=True),
    # Plaintext in Sandbox; the startup guard forbids a non-sandbox boot while it stays that way.
    Column("access_token", Text, nullable=False),
    # The `/transactions/sync` position, persisted per item. NULL before the first sync — Plaid's
    # "sync from the beginning" is the absent cursor, not a sentinel.
    Column("cursor", Text, nullable=True),
    # engine.models.ConnectionState. Ships `healthy`; flips to `login_required` on
    # ITEM_LOGIN_REQUIRED, which is the first thing that drives `ConnectionState` off a constant.
    Column("status", Text, nullable=False, server_default=text("'healthy'")),
    # Becomes `Account.balance_age_days`, the freshness gate — "the thing standing between a
    # dropped webhook and an overdraft" (`architecture.md` [3.1]). NULL until the first success.
    Column("last_successful_sync_at", TIMESTAMP(timezone=True), nullable=True),
    Column("error_code", Text, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "status IN ('healthy', 'login_required', 'disconnected')", name="ck_plaid_items_status"
    ),
)


# Every table whose rows belong to exactly one household. RLS goes on each, the repository scopes
# each, and ticket 0021's IDOR suite proves both — independently.
HOUSEHOLD_SCOPED: tuple[str, ...] = (
    "accounts",
    "cards",
    "policies",
    "decisions",
    "snapshots",
    "spend_projections",
    "plaid_items",
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
    "plaid_items",
    "policies",
    "snapshots",
    "spend_projections",
]
