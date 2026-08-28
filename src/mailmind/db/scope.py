"""Tenant isolation.

07 wants the boundary held below the code that queries: a query that forgets its filter
should return nothing rather than everything.  This is SQLAlchemy's own documented recipe
for that — a ``do_orm_execute`` handler applying :func:`with_loader_criteria` to every
:class:`~mailmind.db.models.TenantScoped` entity, which propagates into subqueries, JOIN
ON clauses and relationship loads without any query having to remember.

What it does *not* cover is anything that is not an ORM statement: Core selects and raw
``text()``.  So raw SQL is confined to migrations and the FTS helper, which takes the
tenant as a bound parameter, and :class:`TenantScope` is the only thing that hands out a
session.

Writes are held by a separate ``before_flush`` handler, because loader criteria say
nothing about INSERT.  It stamps the scope's tenant onto new rows and refuses a row
carrying somebody else's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, with_loader_criteria

from mailmind.db.models import AuditEvent, TenantScoped

#: Key under which the bound tenant lives in ``Session.info``.
TENANT_KEY = "mailmind_tenant_id"


class CrossTenantWrite(Exception):
    """A row was flushed carrying a tenant other than the session's."""


class Unscoped(Exception):
    """A session reached the database without a tenant bound to it."""


class ReadOnlyScope(Exception):
    """A scope opened for reading tried to write.

    Read-only scopes begin DEFERRED and take no write lock, which is only safe if they
    truly do not write — so a write on one is refused here, loudly, instead of quietly
    reintroducing the lost-update race the IMMEDIATE transactions exist to prevent."""


#: Key marking a session as read-only in ``Session.info``.
READONLY_KEY = "mailmind_readonly"


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_criteria(state: Any) -> None:
    if state.is_column_load or state.is_relationship_load:
        # Refreshes and relationship loads already carry the criteria from the statement
        # that produced the parent object; re-applying them is what the recipe warns off.
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return

    tenant_id = state.session.info.get(TENANT_KEY)
    if tenant_id is None:
        if state.execution_options.get("mailmind_unscoped"):
            return
        raise Unscoped(
            "ORM statement issued on a session with no tenant bound. "
            "Use TenantScope, or pass execution_options(mailmind_unscoped=True) "
            "for genuinely tenant-free work such as migrating."
        )

    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _stamp_tenant(session: Session, flush_context: Any, instances: Any) -> None:
    if session.info.get(READONLY_KEY) and (session.new or session.dirty or session.deleted):
        raise ReadOnlyScope("this scope was opened readonly; open a writing scope for changes")
    tenant_id = session.info.get(TENANT_KEY)
    if tenant_id is None:
        return
    for obj in session.new:
        if not isinstance(obj, TenantScoped):
            continue
        current = getattr(obj, "tenant_id", None)
        if current is None:
            obj.tenant_id = tenant_id
        elif current != tenant_id:
            raise CrossTenantWrite(
                f"{type(obj).__name__} carries tenant {current} "
                f"on a session bound to tenant {tenant_id}"
            )
    for obj in session.dirty:
        if isinstance(obj, TenantScoped) and obj.tenant_id != tenant_id:
            raise CrossTenantWrite(f"{type(obj).__name__} would be updated across tenants")


class TenantScope:
    """A session bound to one tenant, and the only way rows are reached.

    Repository functions take a scope rather than a session, so there is no signature in
    the codebase that can be handed an unbound session by accident.  Async throughout —
    the loop owns every transaction; the only threads are the IMAP dips, and they never
    see a session.
    """

    def __init__(self, session: AsyncSession, tenant_id: int) -> None:
        self.session = session
        self.tenant_id = tenant_id
        session.sync_session.info[TENANT_KEY] = tenant_id

    def add(self, obj: Any) -> Any:
        self.session.add(obj)
        return obj

    async def delete(self, obj: Any) -> None:
        """Remove a row that was loaded through this scope, and so is this tenant's."""
        await self.session.delete(obj)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def get(self, entity: type, ident: Any) -> Any:
        return await self.session.get(entity, ident)

    async def scalars(self, statement: Any) -> Any:
        return await self.session.scalars(statement)

    async def all(self, statement: Any) -> list:
        """Every scalar the statement finds — the chainable ``.scalars().all()`` shape."""
        return list((await self.session.scalars(statement)).all())

    async def scalar(self, statement: Any) -> Any:
        return await self.session.scalar(statement)

    async def execute(self, statement: Any) -> Any:
        return await self.session.execute(statement)

    async def audit(
        self,
        verb: str,
        *,
        actor_kind: str,
        subject_kind: str,
        actor_id: int | None = None,
        subject_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append to the record.  Never updated, never deleted."""
        next_seq = (
            await self.session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(AuditEvent.seq), 0)).where(
                    AuditEvent.tenant_id == self.tenant_id
                )
            )
            or 0
        ) + 1
        event_row = AuditEvent(
            seq=next_seq,
            actor_kind=actor_kind,
            actor_id=actor_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            verb=verb,
            payload=payload or {},
        )
        self.session.add(event_row)
        return event_row


def make_sessionmaker(
    engine: AsyncEngine, *, readonly: bool = False
) -> async_sessionmaker[AsyncSession]:
    if readonly:
        return async_sessionmaker(
            engine.execution_options(mailmind_readonly=True),
            expire_on_commit=False,
            info={READONLY_KEY: True},
        )
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_scope(
    sessions: async_sessionmaker[AsyncSession], tenant_id: int
) -> AsyncIterator[TenantScope]:
    async with sessions() as session:
        yield TenantScope(session, tenant_id)


@asynccontextmanager
async def unscoped_session(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """For migrations and administration only — tenancy is not enforced here.

    Every ORM statement issued through it must carry
    ``execution_options(mailmind_unscoped=True)``, which is deliberately noisy.
    """
    async with sessions() as session:
        yield session
