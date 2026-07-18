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
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
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
    # Whether this household is part of the public demo plane (KTD-10, ticket 0046). A `viewer`-role
    # demo user is a member of exactly the `is_demo` households and nothing else, so the public
    # bundle reaches the demo with no login while never touching a real household; and Plaid
    # link-exchange refuses an `is_demo` household, so a real bank item can never attach to the demo
    # plane. Default false: a real household is never a demo one by omission. Set true by the seeder
    # on synthetic households (`backend/seed.py`), backfilled true in migration 0011 for the ones
    # already seeded.
    Column("is_demo", Boolean, nullable=False, server_default=text("false")),
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

# The append-only guardrail history — the source of record for a household's policy (ticket 0049,
# the identity rung's U4, KTD-6). It **replaces** the old mutable `policies` row: policy is
# safety-critical and the whole codebase's identity is append-only auditability, so a change is a
# new event and the current policy is the latest one, read by `seq` (not `created_at`, which a seed
# transaction can tie on — the `transfers.seq` lesson). `Repository.policy()` projects it; nothing
# keeps a second mutable copy that could disagree.
#
# Append-only is a **grant** (SELECT + INSERT only, migration 0012), not a convention — like
# `plaid_transactions` and `transfers`. `changed_by` is the user who made the change (NULL for a
# seeded/initial event); `loosened` marks a change that weakened a guardrail, the audit marker the
# deferred step-up retrofit finds them by (KTD-9).
policy_events = Table(
    "policy_events",
    metadata,
    Column("id", Text, primary_key=True),
    # Monotonic order — `policy()` reads the latest by this, never by `created_at`.
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # The actor. FK to the platform `users` table, ON DELETE SET NULL so a shredded user's audit
    # trail survives with the actor nulled rather than the history erased.
    Column("changed_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("buffer_floor", MONEY, nullable=False),
    Column("max_sweep", MONEY, nullable=False),
    Column("max_weekly_sweep", MONEY, nullable=False),
    # `decision-engine.md` §9. Ships at 7. `0` restores the daily engine exactly, which is why it
    # is a policy value and not a constant.
    Column("min_days_between_sweeps", Integer, nullable=False, server_default=text("7")),
    Column("blackout_dates", ARRAY(Date), nullable=False, server_default=text("'{}'")),
    Column("loosened", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("buffer_floor >= 0", name="ck_policy_events_buffer_nonneg"),
    CheckConstraint("max_sweep >= 0", name="ck_policy_events_max_sweep_nonneg"),
    CheckConstraint("max_weekly_sweep >= 0", name="ck_policy_events_weekly_nonneg"),
    CheckConstraint("min_days_between_sweeps >= 0", name="ck_policy_events_spacing_nonneg"),
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


# Every `/transactions/sync` outcome — added, modified, removed — lands here as an INSERT (ticket
# 0036). **Append-only**: corrections are new rows carrying a `change_type`, never an UPDATE or a
# DELETE (`architecture.md` [4]). The migration enforces it at the privilege layer — `cfo_app` is
# granted SELECT and INSERT and nothing else — so append-only is a guarantee, not a convention.
#
# `plaid_transaction_id` is therefore **not unique**: a legitimate second `modified` of the same
# transaction shares it, and so does a later `removed`. The reconciliation that would collapse them
# is normalization, which this rung defers.
#
# `amount`/`date`/`name`/`merchant_name` are **nullable** because a `removed` event carries only
# ids (`plaid_transaction_id` and `plaid_account_id`) — NULL is the honest absence, not a fabricated
# row. `pending_transaction_id` is a column with no consumer here; the reconciliation that reads it
# is normalization's, not this rung's ([Resolved Decisions], brainstorm [2] U3).
plaid_transactions = Table(
    "plaid_transactions",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("plaid_item_id", Text, nullable=False),
    Column("plaid_account_id", Text, nullable=False),
    Column("plaid_transaction_id", Text, nullable=False),
    # The id of the pending transaction a posted one supersedes. Stored, never yet read — the
    # reconciliation that consumes it is normalization ([3]), out of this rung.
    Column("pending_transaction_id", Text, nullable=True),
    # NUMERIC, never float (ADR-0002 [2.2]). Nullable: a `removed` event carries no amount.
    Column("amount", MONEY, nullable=True),
    Column("date", Date, nullable=True),
    Column("name", Text, nullable=True),
    Column("merchant_name", Text, nullable=True),
    # added | modified | removed — all INSERTs. The CHECK says so where the data lives.
    Column("change_type", Text, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "change_type IN ('added', 'modified', 'removed')",
        name="ck_plaid_transactions_change_type",
    ),
)


# `plaid_accounts` / `plaid_liabilities` (migration 0014): the balance and card-terms snapshots that
# `plaid_transactions` cannot carry — what is *true now*, not what was *done*. Append-only like
# `plaid_transactions` (SELECT/INSERT only; a refresh is a new row); the reader takes the latest per
# account by `seq`, never `fetched_at` (the `policy_events`/`transfers` monotonic-order lesson).
plaid_accounts = Table(
    "plaid_accounts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("plaid_item_id", Text, nullable=False),
    Column("plaid_account_id", Text, nullable=False),
    Column("name", Text, nullable=True),
    Column("official_name", Text, nullable=True),
    # Plaid's type/subtype: depository/checking, depository/savings, credit/credit card.
    Column("type", Text, nullable=True),
    Column("subtype", Text, nullable=True),
    Column("current_balance", MONEY, nullable=True),
    Column("available_balance", MONEY, nullable=True),
    Column("iso_currency_code", Text, nullable=True),
    Column("fetched_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
)


plaid_liabilities = Table(
    "plaid_liabilities",
    metadata,
    Column("id", Text, primary_key=True),
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("plaid_item_id", Text, nullable=False),
    Column("plaid_account_id", Text, nullable=False),
    Column("last_statement_balance", MONEY, nullable=True),
    Column("last_statement_issue_date", Date, nullable=True),
    Column("minimum_payment", MONEY, nullable=True),
    Column("next_payment_due_date", Date, nullable=True),
    # A fraction (0.2399), not a percentage — the engine's `apr` convention; the fetch converts.
    Column("purchase_apr", RATE, nullable=True),
    Column("is_overdue", Boolean, nullable=True),
    Column("fetched_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "purchase_apr IS NULL OR (purchase_apr >= 0 AND purchase_apr <= 2)",
        name="ck_plaid_liabilities_apr_range",
    ),
)


# **The one table deliberately outside `HOUSEHOLD_SCOPED`** (ticket 0035, ADR-0005). Plaid's webhook
# carries `item_id`, not `household_id`, so the doorbell cannot resolve a household before RLS could
# be set — and RLS fails closed, so a scoped insert here would see nothing to match and reject the
# row. The raw store is therefore unscoped, mirroring `GET /households` (`backend/main.py`), and the
# *worker* resolves `plaid_item_id → household_id` through the `SECURITY DEFINER`
# `plaid_household_for_item()` function before it touches any financial row. Both holes — this table
# and that function — are the first exceptions to "every table is scoped by household_id"
# (`architecture.md` [4]), and ADR-0005 is where they are argued and bounded (including retention).
#
# The dedup key is `NULLS NOT DISTINCT`: `cursor` is NULL for the webhook that matters most
# (TRANSACTIONS / SYNC_UPDATES_AVAILABLE carries none), and under default NULL-distinct semantics
# two such redeliveries would both be admitted — the opposite of dedup. NULLS NOT DISTINCT treats
# them as equal so `ON CONFLICT DO NOTHING` drops the redelivery. This is a best-effort guard on the
# *enqueue*; the sync itself is idempotent by cursor regardless.
plaid_webhooks = Table(
    "plaid_webhooks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("plaid_item_id", Text, nullable=False),
    Column("webhook_type", Text, nullable=False),
    Column("webhook_code", Text, nullable=False),
    # Whatever cursor the payload carries, if any. Usually NULL — see the note above.
    Column("cursor", Text, nullable=True),
    Column("payload", JSONB, nullable=False),
    Column("received_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    # The named dedup key; the migration declares it NULLS NOT DISTINCT (not expressible here).
    UniqueConstraint("plaid_item_id", "webhook_code", "cursor", name="uq_plaid_webhooks_dedup"),
)


# The append-only ledger of money movement — the write half (ticket 0039, the sweep-execution rung,
# `docs/plans/2026-07-17-002-feat-sweep-execution-rung-plan.md`). One row per state transition of a
# transfer leg: a debit (ACH pull from checking into a platform funding account) or a payoff (a
# biller-payoff API landing that money on the card). **Append-only, exactly like
# `plaid_transactions`:** a transition is a new row, never an UPDATE — the migration grants
# `cfo_app` SELECT and INSERT and
# nothing else, so history cannot be rewritten even by mistake. `architecture.md` [5] calls the
# idempotency here "the highest-stakes in the system, because a duplicate sweep is an overdraft."
#
# The idempotency guard keys on `(household_id, decision_date)`, **not** `decision_id` — a same-day
# re-decision mints a new `decision_id`, so a `decision_id`-keyed constraint would let a second
# debit slip the guard meant to stop it (KTD-2). It is enforced by a `SELECT … FOR UPDATE` slot lock
# in the saga (U4), not a UNIQUE here — and there is deliberately **no UNIQUE** on
# `provider_transfer_id`, since a status-transition row or a superseding transfer shares it, exactly
# the correction append-only exists to keep (same reasoning as `plaid_transactions`).
#
# `target_card_id` matches the persisted `decisions.target_card_id` column (the engine dataclass
# field is `target_debt_id`; the naming reconciliation is a flagged follow-up, KTD-8).
# `provider_transfer_id` and `return_code` are NULL until `submit` / a return arrives. Shadow mode
# (U2) still writes these
# rows; it just never calls a vendor, so `submit()` is a logged no-op that advances the ledger.
transfers = Table(
    "transfers",
    metadata,
    Column("id", Text, primary_key=True),
    # A monotonic insertion order. `created_at` is `now()` — transaction-start time — so a saga step
    # that appends several rows in one transaction ties on it, and the state machine's whole meaning
    # is the *order* of transitions (which state is latest). `seq` is the total order the
    # latest-state-per-slot query (U4/U5) reads; `created_at` stays for wall-clock reporting.
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # The card this sweep pays down. Not FK'd (like `decisions`, which cannot FK the partitioned
    # table either) — the card lives under `(household_id, id)` and the scope binds the household.
    Column("target_card_id", Text, nullable=False),
    # The decision that authorized this transfer, and the day it was decided. `(household_id,
    # decision_date)` is the idempotency slot; a re-decision reuses it with a new `decision_id`.
    Column("decision_id", Text, nullable=False),
    Column("decision_date", Date, nullable=False),
    # debit (fund the platform account) | payoff (land it on the card).
    Column("leg", Text, nullable=False),
    # The state machine (`architecture.md` [5]). Each transition is a new row.
    Column("state", Text, nullable=False),
    # debit (pull) | credit (push). Increase encodes this as the sign of the amount; the port takes
    # an unsigned amount + this direction and converts (KTD-3).
    Column("direction", Text, nullable=False),
    # NUMERIC, never float (ADR-0002 [2.2]). A transfer always moves a positive amount.
    Column("amount", MONEY, nullable=False),
    # increase (debit leg) | method (payoff leg). The vendor behind the leg; shadow mode still names
    # the intended provider, it just does not call it.
    Column("provider", Text, nullable=False),
    # The vendor's own id for the transfer, once `submit` returns. NULL before submission; **not
    # unique** (a superseding or status-transition row shares it).
    Column("provider_transfer_id", Text, nullable=True),
    # The return/reversal code once one arrives (NACHA R-code for Increase; a Method error code).
    Column("return_code", Text, nullable=True),
    # Derived from the slot + a step suffix (KTD-4), passed as the vendor Idempotency-Key header so
    # a worker retry of the same step reuses it.
    Column("idempotency_key", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("leg IN ('debit', 'payoff')", name="ck_transfers_leg"),
    CheckConstraint(
        "state IN ('proposed', 'authorized', 'submitted', 'pending', 'settled',"
        " 'returned', 'failed', 'cancelled')",
        name="ck_transfers_state",
    ),
    CheckConstraint("direction IN ('debit', 'credit')", name="ck_transfers_direction"),
    # plaid_transfer is the primary debit rail (ADR-0007); increase stays a swappable backup.
    CheckConstraint(
        "provider IN ('plaid_transfer', 'increase', 'method')", name="ck_transfers_provider"
    ),
    CheckConstraint("amount > 0", name="ck_transfers_amount_positive"),
)


# A real, verified user — the identity every route was missing (ticket 0046, the identity rung,
# `docs/plans/2026-07-17-003-feat-identity-and-settings-controls-plan.md`, ADR-0008).
#
# **Deliberately NOT household-scoped** (see `PLATFORM_TABLES` and `HOUSEHOLD_SCOPED` below). A user
# exists before any household and independent of all of them — the same posture as the raw webhook
# store (ADR-0005) and the FBO funding account. So there is no `household_id` here, no RLS policy,
# and it is outside the forced set. Its exclusion is asserted, not incidental
# (`tests/test_schema.py`, `tests/test_identity_schema.py`).
#
# It carries PII (`email`) with no RLS backstop, so the application reaches it **only by single-key
# lookup** — `get_user_by_stytch_id` / `get_user_by_id` in `backend/db/repository.py`, never an
# unfiltered scan or join. There is no `users()` list method by design, and a test enforces it.
#
# `id` is ours and stable — the identifier the deferred Plaid Link rung passes to
# `/link/token/create` as `client_user_id` (KTD-1). `stytch_user_id` is the vendor's; only the edge
# adapter (`backend/identity/stytch.py`) knows it, and a provider swap re-keys only that column.
users = Table(
    "users",
    metadata,
    Column("id", Text, primary_key=True),
    Column("stytch_user_id", Text, nullable=False, unique=True),
    Column("email", Text, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    Column("deleted_at", TIMESTAMP(timezone=True), nullable=True),
)

# The many-to-many bridge between users and the households they may read or write (ticket 0046).
# This is where "which household may this session touch?" is answered — `households_for_user` reads
# it, and the request's `household_id` must be in that set or the route refuses (KTD-2).
#
# Keyed by `household_id`, so it **is** HOUSEHOLD_SCOPED and RLS-forced like every tenant table.
# `user_id` is a bridge FK, not the tenant key — `test_no_table_carries_a_user_id` is scoped to say
# exactly that. `role` is `owner` (may write) or `viewer` (read-only — the public demo principal,
# KTD-10); it is the one role distinction the rung ships.
household_members = Table(
    "household_members",
    metadata,
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column(
        "user_id",
        Text,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column("role", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("role IN ('owner', 'viewer')", name="ck_household_members_role"),
)


# The append-only record of a user's card-completeness attestation (ticket 0050, U5, KTD-7). The
# engine refuses to sweep a household whose card set it cannot be sure is complete; this is the row
# that clears that gate (the 0016 money-gate). `card_fingerprint` is a stable hash of the attested
# card identities — coverage is COMPLETE only while it matches the household's *current* cards, so a
# newly appearing card silently invalidates a stale attestation. Append-only (SELECT + INSERT grant,
# migration 0013), like `policy_events` and `transfers`; `current_attestation()` reads the latest by
# `seq`. It only ever moves UNATTESTED → COMPLETE — `derive_portfolio` keeps UNMATCHED_PAYMENT
# overriding regardless.
card_attestations = Table(
    "card_attestations",
    metadata,
    Column("id", Text, primary_key=True),
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column(
        "household_id",
        Text,
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("attested_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("card_fingerprint", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
)


# Tables that are deliberately NOT scoped by household, each for a named and tested reason. `users`
# is platform-level (a user predates any household, KTD-1); `plaid_webhooks` is item-keyed, not
# household-keyed (ADR-0005). Naming them here makes each exclusion an asserted decision rather than
# an omission — `tests/test_schema.py` checks a scoped table did not quietly land in this set and
# vice versa.
PLATFORM_TABLES: tuple[str, ...] = (
    "users",
    "plaid_webhooks",
)

# Every table whose rows belong to exactly one household. RLS goes on each, the repository scopes
# each, and ticket 0021's IDOR suite proves both — independently. `plaid_webhooks` and `users` are
# deliberately NOT here (see `PLATFORM_TABLES`): a webhook names an item, and a user predates every
# household.
HOUSEHOLD_SCOPED: tuple[str, ...] = (
    "accounts",
    "cards",
    "policy_events",
    "decisions",
    "snapshots",
    "spend_projections",
    "plaid_items",
    "plaid_transactions",
    "plaid_accounts",
    "plaid_liabilities",
    "transfers",
    "household_members",
    "card_attestations",
)

__all__ = [
    "HOUSEHOLD_SCOPED",
    "MONEY",
    "PLATFORM_TABLES",
    "RATE",
    "RLS_VAR",
    "accounts",
    "card_attestations",
    "cards",
    "decisions",
    "household_members",
    "households",
    "metadata",
    "plaid_accounts",
    "plaid_items",
    "plaid_liabilities",
    "plaid_transactions",
    "plaid_webhooks",
    "policy_events",
    "snapshots",
    "spend_projections",
    "transfers",
    "users",
]
