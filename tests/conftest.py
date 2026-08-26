from __future__ import annotations

import pytest
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.engine import create_engine
from mailmind.db.migrate import upgrade_to_head
from mailmind.db.scope import make_sessionmaker, tenant_scope
from mailmind.imap import sync
from mailmind.imap.capabilities import probe_account
from tests.corpus import CORPUS
from tests.targets.fake import FakeBackend

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


DECLARED = ("CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE")


@pytest.fixture
def backend():
    backend = FakeBackend()
    backend.add_folder("INBOX")
    backend.add_folder("Archive", special_use="archive")
    backend.add_folder("Trash", special_use="trash")
    return backend


@pytest.fixture
def world(scope, backend):
    """An account, its containers synced, and a seed map from logical name to message id."""
    account = scope.add(
        m.Account(
            name="test",
            host="imap.invalid",
            username="me@example.org",
            password_url="env://X",
        )
    )
    scope.flush()
    for name in DECLARED:
        scope.add(m.AccountCapability(account_id=account.id, name=name))
    producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
    reviewer = scope.add(m.Producer(kind=m.ProducerKind.person, name="ronny"))
    scope.flush()

    uids = {name: backend.add_message("INBOX", raw) for name, raw in CORPUS.items()}
    probe_account(scope, account, backend)
    containers = {c.name: c for c in sync.discover_containers(scope, account, backend)}
    sync.sync_container(scope, account, containers["INBOX"], backend)
    scope.commit()

    seed = {}
    for name, uid in uids.items():
        placement = scope.scalar(
            sa.select(m.Placement).where(
                m.Placement.container_id == containers["INBOX"].id, m.Placement.uid == uid
            )
        )
        seed[name] = placement.message_id
    return {
        "account": account,
        "producer": producer,
        "reviewer": reviewer,
        "containers": containers,
        "seed": seed,
        "uids": uids,
    }


def accept_as_shown(scope, bundle, reviewer, **kw):
    """Accept the way a person does: from the page they were shown.

    ``reviewed_through`` is required rather than defaulted, so that a review surface
    cannot forget to say what it rendered.  A test is not a review surface, and every
    test that is not *about* the review premise means "the page showed everything".
    """
    from mailmind.suggest import model as suggest

    return suggest.accept(
        scope, bundle, reviewer, reviewed_through=suggest.shown_through(bundle), **kw
    )
