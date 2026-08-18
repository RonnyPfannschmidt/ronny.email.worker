"""Engine construction, with the pragmas the schema assumes."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def create_engine(url: str, *, echo: bool = False) -> Engine:
    engine = sa.create_engine(url, echo=echo)

    @sa.event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # Composite and ordinary foreign keys are both off by default in SQLite.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine
