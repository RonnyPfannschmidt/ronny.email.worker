"""Running migrations from code.

Alembic normally reads ``alembic.ini`` relative to the working directory, which is fine
for a developer and not fine for an installed command or a test. This builds the config
in memory instead, so ``mailmindctl migrate`` and the test suite create their schema the
same way — by running the migrations, not by a second definition of the schema that can
drift away from them.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    # A password-free SQLAlchemy URL can still contain a percent sign, and ConfigParser
    # would read it as interpolation.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def downgrade_to(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


class SchemaBehind(Exception):
    """The database was built by an older version of this code than is running."""


def current_revision(url: str) -> str | None:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def head_revision(url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


def schema_problem(url: str) -> str | None:
    """Why this build cannot operate against this database, or None.

    The non-raising shape of :func:`require_current_schema`, for callers that hold the
    answer up rather than dying of it — the 503 mode, the runner's drift check.
    """
    try:
        require_current_schema(url)
    except SchemaBehind as exc:
        return str(exc)
    return None


def require_current_schema(url: str) -> None:
    """Refuse to touch a database this code no longer matches.

    The alternative is what happened: a service running from a checkout that had moved on,
    a column the schema did not have, and a SQLAlchemy traceback in the middle of a sync
    saying `no such column`. Which is true, and says nothing about what to do.

    Migrating here instead would mean every command quietly rewriting somebody's mail
    cache the first time it ran — 0004 rewrites rows, not only tables. `migrate` is where
    that happens, because that is the whole of what it is for.
    """
    head = head_revision(url)
    current = current_revision(url)
    if current == head:
        return
    raise SchemaBehind(
        f"this database is at {current or 'no revision'} and this build needs {head} — "
        "run `mailmindctl migrate` to bring it up to date"
    )
