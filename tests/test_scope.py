"""Does the boundary actually hold?

07 asks for one thing: a query that forgets its filter returns nothing rather than
everything.  These tests are that claim, written so it fails loudly if the recipe stops
covering a case.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import (
    CrossTenantWrite,
    Unscoped,
    tenant_scope,
    unscoped_session,
)


@pytest.fixture
def two_tenants(sessions):
    """Two tenants, each with an account, a container and a message in it."""
    with unscoped_session(sessions) as session:
        # Tenant zero comes from the migration; these two are its neighbours.
        for name in ("zero", "one"):
            session.add(m.Tenant(name=name))
        session.commit()
        ids = {
            t.name: t.id
            for t in session.scalars(
                sa.select(m.Tenant).execution_options(mailmind_unscoped=True)
            )
        }

    for name, tenant_id in ids.items():
        with tenant_scope(sessions, tenant_id) as scope:
            account = scope.add(
                m.Account(
                    name=f"{name}-account",
                    host="imap.example",
                    username=f"{name}@example",
                    password_url="env://NOPE",
                )
            )
            scope.flush()
            container = scope.add(m.Container(account_id=account.id, name="INBOX"))
            scope.flush()
            message = scope.add(
                m.Message(account_id=account.id, content_key=f"{name}-key", subject=f"{name}")
            )
            scope.flush()
            scope.add(
                m.Placement(
                    message_id=message.id,
                    container_id=container.id,
                    uid=1,
                    container_generation=1,
                )
            )
            scope.commit()
    return ids


def test_a_query_that_forgets_its_filter_returns_nothing(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        # No WHERE tenant_id anywhere in this statement.
        subjects = scope.scalars(sa.select(m.Message.subject)).all()
    assert subjects == ["zero"]


def test_relationship_loads_do_not_leak(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        accounts = scope.scalars(sa.select(m.Account)).all()
        assert len(accounts) == 1
        containers = accounts[0].containers
        assert [c.name for c in containers] == ["INBOX"]
        # The join target is filtered too, not only the outer entity.
        joined = scope.scalars(
            sa.select(m.Placement).join(m.Container, m.Placement.container_id == m.Container.id)
        ).all()
        assert len(joined) == 1


def test_get_by_primary_key_cannot_reach_another_tenant(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["one"]) as scope:
        other = scope.scalar(sa.select(m.Message.id).where(m.Message.subject == "one"))
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        assert scope.get(m.Message, other) is None


def test_an_unbound_session_refuses_to_query(sessions, two_tenants):
    with sessions() as session:
        with pytest.raises(Unscoped):
            session.scalars(sa.select(m.Message)).all()


def test_writing_another_tenants_row_is_refused(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        scope.add(m.Producer(tenant_id=two_tenants["one"], kind=m.ProducerKind.agent, name="x"))
        with pytest.raises(CrossTenantWrite):
            scope.flush()


def test_tenant_is_stamped_without_being_named(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        scope.commit()
        assert producer.tenant_id == two_tenants["zero"]


def test_orm_delete_is_scoped(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        scope.execute(sa.delete(m.Placement))
        scope.commit()
    with tenant_scope(sessions, two_tenants["one"]) as scope:
        assert scope.scalars(sa.select(sa.func.count()).select_from(m.Placement)).one() == 1


def test_orm_update_is_scoped(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        scope.execute(sa.update(m.Message).values(subject="rewritten"))
        scope.commit()
    with tenant_scope(sessions, two_tenants["one"]) as scope:
        assert scope.scalars(sa.select(m.Message.subject)).all() == ["one"]


def test_every_scoped_table_carries_a_tenant(sessions):
    """A new table that forgets the mixin is visible across tenants."""
    unscoped = [
        mapper.class_.__name__
        for mapper in m.Base.registry.mappers
        if not issubclass(mapper.class_, m.TenantScoped)
    ]
    assert unscoped == ["Tenant"]


def test_no_grant_can_carry_apply(sessions, two_tenants):
    """Applying is absent from the agent side, not denied on it."""
    assert not any("apply" in c.value for c in m.Capability)


def test_audit_events_are_sequenced_per_tenant(sessions, two_tenants):
    for name in ("zero", "one"):
        with tenant_scope(sessions, two_tenants[name]) as scope:
            for verb in ("first", "second"):
                scope.audit(verb, actor_kind="service", subject_kind="tenant")
            scope.commit()
    with tenant_scope(sessions, two_tenants["one"]) as scope:
        events = scope.scalars(sa.select(m.AuditEvent).order_by(m.AuditEvent.seq)).all()
    assert [(e.seq, e.verb) for e in events] == [(1, "first"), (2, "second")]


def test_a_stored_time_comes_back_comparable_to_now(sessions, two_tenants):
    """SQLite has no offset to store, so an aware datetime came back naive.

    Every column here is declared ``timezone=True`` and none of them were, which turns the
    ordinary act of comparing a stored time to ``now()`` into a TypeError.
    """
    written = dt.datetime(2026, 8, 19, 11, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        scope.flush()
        scope.add(
            m.Grant(
                producer_id=producer.id,
                token_hash="not-a-real-token",
                capabilities=["observe"],
                expires_at=written,
            )
        )
        scope.commit()

    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        grant = scope.scalar(sa.select(m.Grant))
        assert grant.expires_at.tzinfo is not None
        assert grant.expires_at == written, "the offset was dropped instead of applied"
        assert grant.expires_at <= dt.datetime.now(dt.UTC)
        assert grant.created_at <= dt.datetime.now(dt.UTC)


def test_concurrent_writers_do_not_collide_on_the_audit_sequence(sessions, two_tenants):
    """Every route in the review UI is a ``def``, so starlette runs them in a threadpool,
    and the MCP tools run in worker threads of their own.

    Two transactions therefore read the same ``MAX(seq)``, both wrote ``seq + 1``, and the
    second died on the unique constraint — taking down not just its audit line but the
    sync or the acceptance that line was recording.
    """
    tenant_id = two_tenants["zero"]
    rounds, workers = 20, 4
    failures: list[str] = []

    def write_audits() -> None:
        for _ in range(rounds):
            try:
                with tenant_scope(sessions, tenant_id) as scope:
                    scope.audit("touched", actor_kind="service", subject_kind="tenant")
                    scope.commit()
            except Exception as exc:  # noqa: BLE001 — the failure is the finding
                failures.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=write_audits) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    with tenant_scope(sessions, tenant_id) as scope:
        seqs = list(scope.scalars(sa.select(m.AuditEvent.seq).order_by(m.AuditEvent.seq)))
    assert seqs == list(range(1, rounds * workers + 1))


def test_move_bundle_requires_a_target(sessions, two_tenants):
    with tenant_scope(sessions, two_tenants["zero"]) as scope:
        account_id = scope.scalar(sa.select(m.Account.id))
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        scope.flush()
        scope.add(
            m.Bundle(
                account_id=account_id,
                producer_id=producer.id,
                action_kind=m.ActionKind.state,
                operation=m.Operation.move,
                target_container_id=None,
                summary="s",
                reason="r",
                expires_at=dt.datetime.now(dt.UTC),
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            scope.commit()
