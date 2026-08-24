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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria

from mailmind.db.models import AuditEvent, TenantScoped

#: Key under which the bound tenant lives in ``Session.info``.
TENANT_KEY = "mailmind_tenant_id"


class CrossTenantWrite(Exception):
    """A row was flushed carrying a tenant other than the session's."""


class Unscoped(Exception):
    """A session reached the database without a tenant bound to it."""


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
    the codebase that can be handed an unbound session by accident.
    """

    def __init__(self, session: Session, tenant_id: int) -> None:
        self.session = session
        self.tenant_id = tenant_id
        session.info[TENANT_KEY] = tenant_id

    def add(self, obj: Any) -> Any:
        self.session.add(obj)
        return obj

    def delete(self, obj: Any) -> None:
        """Remove a row that was loaded through this scope, and so is this tenant's."""
        self.session.delete(obj)

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def get(self, entity: type, ident: Any) -> Any:
        return self.session.get(entity, ident)

    def scalars(self, statement: Any) -> Any:
        return self.session.scalars(statement)

    def scalar(self, statement: Any) -> Any:
        return self.session.scalar(statement)

    def execute(self, statement: Any) -> Any:
        return self.session.execute(statement)

    def audit(
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
            self.session.scalar(
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


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def tenant_scope(sessions: sessionmaker[Session], tenant_id: int) -> Iterator[TenantScope]:
    with sessions() as session:
        yield TenantScope(session, tenant_id)


@contextmanager
def unscoped_session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    """For migrations and administration only — tenancy is not enforced here.

    Every ORM statement issued through it must carry
    ``execution_options(mailmind_unscoped=True)``, which is deliberately noisy.
    """
    with sessions() as session:
        yield session
