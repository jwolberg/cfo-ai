"""Alembic environment.

The URL comes from `DATABASE_URL`, never from `alembic.ini`. A connection string in a committed
ini file is a credential in the repo, and `architecture.md` [4] is explicit that Plaid tokens —
and by extension anything that reaches them — do not live in plaintext beside everything else.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.db.models import metadata

config = context.config
config.set_main_option("sqlalchemy.url", __import__("os").environ["DATABASE_URL"])

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
