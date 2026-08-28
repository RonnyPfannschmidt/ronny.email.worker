"""The runtime: one database, one set of mailbox connections, shared by both surfaces.

The MCP endpoint and the review UI are the same process deliberately.  They see the same
cache and the same suggestions, and the boundary between them is which functions each can
reach — the applier is imported by the review flow and by nothing on the agent side.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mailmind.config import AccountConfig, Config, Login, load_config
from mailmind.db import models as m
from mailmind.db.engine import create_engine_async
from mailmind.db.scope import TenantScope, make_sessionmaker, tenant_scope
from mailmind.imap.backend import MailBackend

#: This iteration has one tenant.  Everything is written as though there were more,
#: because retrofitting the boundary later is the way to get it wrong.
TENANT_ZERO = 0


def account_config(account: m.Account) -> AccountConfig:
    """What a connection needs, taken from the row rather than from the file.

    The row is the source of truth.  Configured accounts are seeded into it by the
    seeding, and an account added through the review UI will only ever exist as one —
    looking the name back up in the configuration would have made such an account exist
    and be unusable at the same time.

    ``caps`` is absent because nothing building a connection reads it: what a server is
    declared to do lives in ``account_capability`` rows, and the probe compares those.
    """
    return AccountConfig(
        name=account.name,
        host=account.host,
        port=account.port,
        use_ssl=account.use_ssl,
        login=Login(username=account.username, password=account.password_url),
        cache_bodies=account.cache_bodies,
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token() -> str:
    return secrets.token_urlsafe(32)


class Service:
    def __init__(
        self,
        config: Config | None = None,
        *,
        backend_factory: Callable[[AccountConfig], MailBackend] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.engine = create_engine_async(self.config.database_url)
        self.sessions = make_sessionmaker(self.engine)
        #: The same pool, begun DEFERRED: reads that neither wait on a sync nor block it.
        self.readers = make_sessionmaker(self.engine, readonly=True)
        self._backend_factory = backend_factory or _real_backend
        self._backends: dict[str, MailBackend] = {}
        self._backend_locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = threading.Lock()
        #: Set by the task runner at startup; a no-op until one is running.
        self.notify_tasks: Callable[[], None] = lambda: None
        #: Why the service cannot operate, when it cannot — the runner's drift check
        #: sets it if the database is migrated under a live process, and every request
        #: then answers 503 with this text instead of a traceback from inside a sync.
        self.schema_problem: str | None = None

    @asynccontextmanager
    async def scope(
        self, tenant_id: int = TENANT_ZERO, *, readonly: bool = False
    ) -> AsyncIterator[TenantScope]:
        sessions = self.readers if readonly else self.sessions
        async with tenant_scope(sessions, tenant_id) as scope:
            yield scope

    def run(self, main):  # noqa: ANN001, ANN201
        """``asyncio.run`` with the pool disposed before the loop goes.

        A pooled aiosqlite connection belongs to the loop that made it, so everything
        that runs this service on a loop of its own — a CLI command, a test helper —
        goes through here and hands the pool back clean.
        """

        async def go():  # noqa: ANN202
            try:
                return await main
            finally:
                await self.engine.dispose()

        return asyncio.run(go())

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def backend(self, account: m.Account) -> AsyncIterator[MailBackend]:
        """One account's connection, held for as long as the caller needs it.

        A backend is handed out under a per-account lock rather than returned, because
        IMAP is a stateful protocol with a selected folder, and a connection therefore
        belongs to one logical sequence at a time.  Everything that talks IMAP runs on
        the one loop, so the lock is an ``asyncio.Lock``; the blocking calls themselves
        happen in thread dips that only ever carry plain data.
        """
        with self._registry_lock:
            backend = self._backends.get(account.name)
            if backend is None:
                backend = self._backends[account.name] = self._backend_factory(
                    account_config(account)
                )
            lock = self._backend_locks.setdefault(account.name, asyncio.Lock())
        async with lock:
            yield backend

    def close(self) -> None:
        with self._registry_lock:
            backends = list(self._backends.values())
            self._backends.clear()
            self._backend_locks.clear()
        for backend in backends:
            backend.close()


def _real_backend(account: AccountConfig) -> MailBackend:
    from mailmind.imap.client import ImapBackend

    return ImapBackend(account)
