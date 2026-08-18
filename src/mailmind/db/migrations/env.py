"""Alembic environment.

The database URL comes from mailmind's own config rather than alembic.ini, so there is
one place a deployment says where its data lives.
"""

from __future__ import annotations

from alembic import context

from mailmind.config import load_config
from mailmind.db.engine import create_engine
from mailmind.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return (
        context.get_x_argument(as_dictionary=True).get("db_url") or load_config().database_url
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config_engine = create_engine(_url())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER much; batch mode rewrites the table instead.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    config_engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
