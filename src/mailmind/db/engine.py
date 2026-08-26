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
        cursor.execute("PRAGMA busy_timeout=5000")
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
        Serialising a single-user local service costs nothing that matters.
        """
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_engine_async(url: str, *, echo: bool = False) -> AsyncEngine:
    """The async engine the service runs on.

    The pragmas and ``BEGIN IMMEDIATE`` attach to the wrapped sync engine, exactly as
    :func:`create_engine` sets them — one place would be better, and is where this and
    that share their listeners.
    """
    # NullPool: a pooled aiosqlite connection is bound to the event loop that created
    # it, and this engine outlives loops — fixtures, `asyncio.run` CLI commands and the
    # serving loop would otherwise trade poisoned connections through the pool.
    engine = create_async_engine(async_url(url), echo=echo, poolclass=sa.pool.NullPool)
    _install_sqlite_listeners(engine.sync_engine)
    return engine
