from __future__ import annotations

import pytest
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.engine import create_engine
from mailmind.db.scope import make_sessionmaker, tenant_scope, unscoped_session

TENANT_ZERO = 0


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE VIRTUAL TABLE message_fts USING fts5("
                "subject, from_text, preview, message_id UNINDEXED, "
                "tenant_id UNINDEXED, account_id UNINDEXED)"
            )
        )
    sessions = make_sessionmaker(engine)
    with unscoped_session(sessions) as session:
        session.add(m.Tenant(id=TENANT_ZERO, name="tenant-zero"))
        session.commit()
    return sessions


@pytest.fixture
def scope(sessions):
    with tenant_scope(sessions, TENANT_ZERO) as scope:
        yield scope
