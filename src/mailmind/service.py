"""The runtime: one database, one set of mailbox connections, shared by both surfaces.

The MCP endpoint and the review UI are the same process deliberately.  They see the same
cache and the same suggestions, and the boundary between them is which functions each can
reach — the applier is imported by the review flow and by nothing on the agent side.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import sqlalchemy as sa

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

    @contextmanager
    def scope(self, tenant_id: int = TENANT_ZERO) -> Iterator[TenantScope]:
        with tenant_scope(self.sessions, tenant_id) as scope:
            yield scope

    def backend(self, account: m.Account) -> MailBackend:
        if account.name not in self._backends:
            self._backends[account.name] = self._backend_factory(
                self.config.account(account.name)
            )
        return self._backends[account.name]

    def close(self) -> None:
        for backend in self._backends.values():
            backend.close()
        self._backends.clear()

    # ------------------------------------------------------------------- grants

    def resolve_grant(self, token: str) -> m.Grant | None:
        """A token is the whole of what a caller may claim.

        05: an agent cannot widen its own scope, cannot name a tenant, and cannot assert
        who it is.  All of that is settled here, before it says anything.
        """
        with self.scope() as scope:
            grant = scope.scalar(
                sa.select(m.Grant).where(m.Grant.token_hash == hash_token(token))
            )
            if grant is None or grant.revoked_at is not None:
                return None
            if grant.expires_at is not None:
                import datetime as dt

                if grant.expires_at <= dt.datetime.now(dt.UTC):
                    return None
            # Detach the useful parts; the session closes with this block.
            scope.session.expunge_all()
            return grant


def _real_backend(account: AccountConfig) -> MailBackend:
    from mailmind.imap.client import ImapBackend

    return ImapBackend(account)
