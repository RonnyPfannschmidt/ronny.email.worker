"""Alembic environment.

The database URL comes from mailmind's own configuration rather than from alembic.ini, so
one deployment says where its data lives in one place.
"""

from __future__ import annotations

import sqlalchemy as sa
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


class ForeignKeysBroken(Exception):
    """A migration left a reference pointing at a row that is not there."""


def _relax_foreign_keys(engine) -> None:  # noqa: ANN001
    """Stop SQLite enforcing foreign keys while the tables are being rebuilt.

    Batch mode rebuilds a table by copying it, dropping the original and renaming the
    copy.  SQLite will not drop a table another one references — which is every table
    worth migrating — so the rebuild cannot happen with enforcement on.  This is SQLite's
    own documented procedure for the schema changes ALTER TABLE cannot make.

    ``PRAGMA defer_foreign_keys`` is not the answer, though it is the one that looks like
    it: it counts the violations the DROP causes and nothing decrements the count when the
    copy is renamed back into place, so a correct migration fails at commit.

    Turning the check off is only safe because it is turned back on afterwards and the
    whole database is then checked — see :func:`_check_foreign_keys`.  Skipping that would
    make this a way to write a broken database quietly.
    """
    if engine.dialect.name != "sqlite":
        return

    @sa.event.listens_for(engine, "connect")
    def _off(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        # Runs after the pragma listener in mailmind.db.engine, and overrides it for the
        # life of this migration's connection only.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()


def _check_foreign_keys(connection) -> None:  # noqa: ANN001
    """Every reference in the database, checked at once, now the migration is done.

    This is the price of having turned enforcement off, and it is worth paying rather
    than trusting: a rebuilt table that lost a row takes its referencing rows with it, and
    the failure would otherwise turn up much later as a dangling id nothing can explain.
    """
    if connection.dialect.name != "sqlite":
        return
    broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if broken:
        rows = ", ".join(f"{row[0]} row {row[1]} -> {row[2]}" for row in broken[:10])
        raise ForeignKeysBroken(
            f"the migration left {len(broken)} references pointing at rows that are not "
            f"there: {rows}. The database has not been changed."
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
    engine = create_engine(_url())
    _relax_foreign_keys(engine)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # SQLite cannot ALTER much, so batch mode rewrites the table instead:
                # copy to a new table, drop the old one, rename.  Prefer a plain
                # RENAME COLUMN or ADD COLUMN where SQLite supports one — see 0001initial.
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                _check_foreign_keys(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
