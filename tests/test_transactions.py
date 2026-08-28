"""The operational contract of the database layer.

One pooled engine; writers serialized with `BEGIN IMMEDIATE`; readers begun DEFERRED so
under WAL they neither wait on a long write nor make one wait; a read-only scope that
tries to write is refused loudly; and no sync transaction outlives one fetch batch.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import ReadOnlyScope
from mailmind.imap import sync


async def test_the_pool_is_a_pool(scope, sessions):
    """NullPool was the mistake this file exists to keep out: a connection per
    transaction, each with its own thread and PRAGMA round."""
    await scope.scalar(sa.select(m.Tenant.id))
    await scope.commit()
    engine = sessions.kw["bind"]
    assert engine.pool.checkedin() + engine.pool.checkedout() >= 1


async def test_a_reader_is_not_blocked_by_a_writer(sessions, scope, world):
    """The queue page must render while a first sync holds the write lock."""
    from mailmind.db.scope import make_sessionmaker, tenant_scope

    # A writer holding BEGIN IMMEDIATE mid-transaction, uncommitted.
    scope.add(m.Producer(kind=m.ProducerKind.agent, name="holds-the-lock"))
    await scope.flush()  # the write lock is now held by this open transaction

    readers = make_sessionmaker(sessions.kw["bind"], readonly=True)

    async def read_deferred() -> int:
        async with tenant_scope(readers, 0) as reader:
            return await asyncio.wait_for(
                reader.scalar(sa.select(sa.func.count()).select_from(m.Message)), 5
            )

    assert await read_deferred() > 0, "a DEFERRED read proceeds under a held write lock"
    await scope.rollback()


async def test_a_readonly_scope_refuses_to_write(sessions, world):
    from mailmind.db.scope import make_sessionmaker, tenant_scope

    readers = make_sessionmaker(sessions.kw["bind"], readonly=True)
    async with tenant_scope(readers, 0) as reader:
        reader.add(m.Producer(kind=m.ProducerKind.agent, name="smuggled"))
        with pytest.raises(ReadOnlyScope):
            await reader.flush()


async def test_no_sync_transaction_outlives_a_batch(scope, world, backend, monkeypatch):
    """A first sync of a long folder commits as it goes: cut it down mid-way and the
    batches that finished are already durable."""
    for extra in range(sync.FETCH_BATCH * 2):
        backend.add_message(
            "INBOX",
            f"Subject: filler {extra}\r\nFrom: bulk@example.org\r\n\r\nx".encode(),
        )

    real = backend.fetch_envelopes
    calls = 0

    def dies_on_the_third(container, uids):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("the connection died mid-folder")
        return real(container, uids)

    monkeypatch.setattr(backend, "fetch_envelopes", dies_on_the_third)

    container = world["containers"]["INBOX"]
    with pytest.raises(RuntimeError):
        await sync.sync_container(scope, world["account"], container, backend, force_full=True)
    await scope.rollback()

    survived = await scope.scalar(sa.select(sa.func.count()).select_from(m.Message))
    assert survived >= sync.FETCH_BATCH, "the finished batches were thrown away"
