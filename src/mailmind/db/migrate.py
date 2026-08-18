"""Running migrations from code.

Alembic normally reads ``alembic.ini`` relative to the working directory, which is fine
for a developer and not fine for an installed command or a test. This builds the config
in memory instead, so ``mailmindctl bootstrap`` and the test suite create their schema the
same way — by running the migrations, not by a second definition of the schema that can
drift away from them.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

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
