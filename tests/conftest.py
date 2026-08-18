from __future__ import annotations

import pytest

from mailmind.db.engine import create_engine
from mailmind.db.migrate import upgrade_to_head
from mailmind.db.scope import make_sessionmaker, tenant_scope

TENANT_ZERO = 0


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'mailmind.db'}"


@pytest.fixture
def sessions(database_url):
    """A database built by running the migrations.

    Not by ``create_all``: a second way of building the schema is a second definition of
    it, and the two drift silently — the FTS index and tenant zero exist only in the
    migration, so a suite that bypassed it would be testing something the service never
    runs on.  It also means every test run exercises the migration.
    """
    upgrade_to_head(database_url)
    return make_sessionmaker(create_engine(database_url))


@pytest.fixture
def scope(sessions):
    with tenant_scope(sessions, TENANT_ZERO) as scope:
        yield scope
