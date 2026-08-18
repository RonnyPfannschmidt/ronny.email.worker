"""Alembic environment.

The database URL comes from mailmind's own configuration rather than from alembic.ini, so
one deployment says where its data lives in one place.
"""

from __future__ import annotations

from alembic import context

from mailmind.config import load_config
from mailmind.db.engine import create_engine
from mailmind.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    """Where the database is, in order of how explicitly it was said.

    ``-x db_url=...`` beats a url set on the config object, which beats mailmind's own
    configuration.  The last of those is a fallback and not a default to rely on: a
    programmatic caller that means a particular database must say so, or it will quietly
    migrate whichever one the working directory implies.
    """
    from_argument = context.get_x_argument(as_dictionary=True).get("db_url")
    if from_argument:
        return from_argument
    configured = context.config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    return load_config().database_url


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
    engine = create_engine(_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # SQLite cannot ALTER much, so batch mode rewrites the table instead.
                # Note that it drops and recreates, which fails against inbound foreign
                # keys — see 0001initial for why a plain RENAME COLUMN is preferred where
                # SQLite supports one.
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
