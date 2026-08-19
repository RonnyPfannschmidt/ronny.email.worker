"""The runtime: one database, one set of mailbox connections, shared by both surfaces.

The MCP endpoint and the review UI are the same process deliberately.  They see the same
cache and the same suggestions, and the boundary between them is which functions each can
reach — the applier is imported by the review flow and by nothing on the agent side.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from mailmind.config import AccountConfig, Config, load_config
from mailmind.db import models as m
from mailmind.db.engine import create_engine
from mailmind.db.scope import TenantScope, make_sessionmaker, tenant_scope
from mailmind.imap.backend import MailBackend

#: This iteration has one tenant.  Everything is written as though there were more,
#: because retrofitting the boundary later is the way to get it wrong.
TENANT_ZERO = 0


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
        self.engine = create_engine(self.config.database_url)
        self.sessions = make_sessionmaker(self.engine)
        self._backend_factory = backend_factory or _real_backend
        self._backends: dict[str, MailBackend] = {}
        self._backend_locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    @contextmanager
    def scope(self, tenant_id: int = TENANT_ZERO) -> Iterator[TenantScope]:
        with tenant_scope(self.sessions, tenant_id) as scope:
            yield scope

    @contextmanager
    def backend(self, account: m.Account) -> Iterator[MailBackend]:
        """One account's connection, held for as long as the caller needs it.

        A backend is handed out under a lock rather than returned, because IMAP is a
        stateful protocol with a selected folder and a connection therefore belongs to
        one worker at a time — which the routes and the MCP tools, running in threadpools
        of their own, would otherwise not respect.  Two threads interleaving SELECTs on
        one connection do not fail; they read the wrong folder.
        """
        with self._registry_lock:
            backend = self._backends.get(account.name)
            if backend is None:
                backend = self._backends[account.name] = self._backend_factory(
                    self.config.account(account.name)
                )
            lock = self._backend_locks.setdefault(account.name, threading.Lock())
        with lock:
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
