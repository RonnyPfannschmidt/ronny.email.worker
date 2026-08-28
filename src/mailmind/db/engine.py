"""Engine construction, with the pragmas and the transaction mode the schema assumes."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def async_url(url: str) -> str:
    """The same database, addressed through the async driver."""
    if url.startswith("sqlite+aiosqlite:"):
        return url
    if url.startswith("sqlite:"):
        return url.replace("sqlite:", "sqlite+aiosqlite:", 1)
    return url


def create_engine(url: str, *, echo: bool = False) -> Engine:
    engine = sa.create_engine(url, echo=echo)
    _install_sqlite_listeners(engine)
    return engine


def _install_sqlite_listeners(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @sa.event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        # pysqlite opens and commits transactions on its own schedule, which is what
        # makes the explicit BEGIN below impossible while it is in charge of them.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        # Composite and ordinary foreign keys are both off by default in SQLite.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # 15s, not 5: writers are serialized on purpose (BEGIN IMMEDIATE below), and a
        # first sync of a long folder legitimately holds the lock for a while — a waiter
        # that gives up at 5s turns that into `database is locked` errors in whatever
        # else was running.  Waiting is the correct behaviour for a single-user service;
        # the follow-up that shrinks the waits themselves is committing long syncs in
        # smaller pieces.
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    @sa.event.listens_for(engine, "begin")
    def _begin_immediate(connection) -> None:  # noqa: ANN001
        """Take the write lock up front, so read-modify-write is safe between requests.

        Requests, MCP calls and worker-task transactions interleave on the loop (and
        aiosqlite connections each run on a thread of their own).  With SQLite's
        default deferred BEGIN two transactions can read the same row, both decide
        what to write from it, and the second one to commit either loses the race
        silently or dies on a constraint — which is what the audit sequence did,
        taking the whole transaction that carried it down.

        ``BEGIN IMMEDIATE`` makes them queue instead: the second transaction waits for
        the first (up to ``busy_timeout``) and then reads what it actually committed.
        Serialising the *writers* of a single-user local service costs nothing that
        matters — but only the writers.  Under WAL a reader needs no lock at all, so a
        read-only scope (the queue page, the agent's listings) begins DEFERRED and
        neither waits on a long sync nor makes anybody wait; the scope layer refuses to
        flush writes on one, so mislabeling is loud rather than a quiet return of the
        lost-update race.
        """
        if connection.get_execution_options().get("mailmind_readonly", False):
            connection.exec_driver_sql("BEGIN DEFERRED")
        else:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_engine_async(url: str, *, echo: bool = False) -> AsyncEngine:
    """The async engine the service runs on.

    The pragmas and ``BEGIN IMMEDIATE`` attach to the wrapped sync engine, exactly as
    :func:`create_engine` sets them — one place would be better, and is where this and
    that share their listeners.
    """
    # Pooled: opening a SQLite connection is a file open, three PRAGMAs and an
    # aiosqlite thread, and paying that per transaction was the operational mistake
    # NullPool used to make here.  The price of the pool is that an aiosqlite
    # connection is bound to the event loop that created it — so every place that owns
    # a loop end (`Service.run`, the app lifespan, test fixtures) disposes the engine
    # before the loop goes, and the pool refills lazily on the next one.
    engine = create_async_engine(async_url(url), echo=echo)
    _install_sqlite_listeners(engine.sync_engine)
    return engine
