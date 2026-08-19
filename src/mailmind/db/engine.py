"""Engine construction, with the pragmas and the transaction mode the schema assumes."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def create_engine(url: str, *, echo: bool = False) -> Engine:
    engine = sa.create_engine(url, echo=echo)
    if engine.dialect.name != "sqlite":
        return engine

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

        Every FastAPI route here is a ``def`` rather than an ``async def``, so starlette
        runs them concurrently in a threadpool, and the MCP tools run in worker threads
        of their own.  With SQLite's default deferred BEGIN two of those can read the
        same row, both decide what to write from it, and the second one to commit either
        loses the race silently or dies on a constraint — which is what the audit
        sequence did, taking the whole transaction that carried it down.

        ``BEGIN IMMEDIATE`` makes them queue instead: the second transaction waits for
        the first (up to ``busy_timeout``) and then reads what it actually committed.
        Serialising a single-user local service costs nothing that matters.
        """
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
