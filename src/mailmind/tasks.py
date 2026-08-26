"""The task queue's data half: rows in, rows out, no runner in sight.

Everything here is ordinary async domain code over a :class:`TenantScope`; the runner
that claims and executes lives in :mod:`mailmind.worker`.  Enqueuing coalesces, so
asking twice for the same work is the same ask — a second "sync now" while one is
running changes nothing, and a retry button cannot stack retries.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import TenantScope

#: Statuses that make a (kind, account, subject) ask already-in-flight.
LIVE = (m.TaskStatus.queued, m.TaskStatus.running)


async def enqueue(
    scope: TenantScope,
    *,
    kind: m.TaskKind,
    account_id: int,
    subject_id: int,
    payload: dict[str, Any] | None = None,
    requested_by: int | None = None,
) -> tuple[m.Task, bool]:
    """A task row, new or the live one this ask coalesces onto.

    Returns ``(task, created)``.  The caller commits and then calls
    ``service.notify_tasks()`` — in that order, so the runner never wakes to a row a
    rollback took away.
    """
    live = await scope.scalar(
        sa.select(m.Task).where(
            m.Task.kind == kind,
            m.Task.account_id == account_id,
            m.Task.subject_id == subject_id,
            m.Task.status.in_(LIVE),
        )
    )
    if live is not None:
        return live, False
    task = m.Task(
        kind=kind,
        account_id=account_id,
        subject_id=subject_id,
        payload=payload or {},
    )
    if requested_by is not None:
        task.requested_by = requested_by
    scope.add(task)
    await scope.flush()
    await scope.audit(
        "task_enqueued",
        actor_kind="service",
        subject_kind="task",
        subject_id=task.id,
        payload={"kind": kind.value, "account_id": account_id, "subject_id": subject_id},
    )
    return task, True


async def claim_next(scope: TenantScope, *, busy_accounts: set[int]) -> m.Task | None:
    """The oldest queued task whose account has no lane running, flipped to running.

    Single consumer by design: only the one dispatcher calls this, so there is no lease
    to take and nothing to race — ``BEGIN IMMEDIATE`` already serializes it against
    enqueuers.  Caller commits.
    """
    stmt = sa.select(m.Task).where(m.Task.status == m.TaskStatus.queued)
    if busy_accounts:
        stmt = stmt.where(m.Task.account_id.not_in(busy_accounts))
    task = await scope.scalar(stmt.order_by(m.Task.id).limit(1))
    if task is None:
        return None
    task.status = m.TaskStatus.running
    task.started_at = dt.datetime.now(dt.UTC)
    task.attempts += 1
    await scope.audit(
        "task_started",
        actor_kind="service",
        subject_kind="task",
        subject_id=task.id,
        payload={"kind": task.kind.value, "attempt": task.attempts},
    )
    return task


async def finish(
    scope: TenantScope,
    task: m.Task,
    status: m.TaskStatus,
    *,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Done or failed, with what there is to show for it.  Caller commits."""
    task.status = status
    task.error = error
    if result is not None:
        task.result = result
    task.finished_at = dt.datetime.now(dt.UTC)
    await scope.audit(
        "task_finished",
        actor_kind="service",
        subject_kind="task",
        subject_id=task.id,
        payload={"kind": task.kind.value, "status": status.value, "error": error},
    )


async def requeue_interrupted(scope: TenantScope) -> int:
    """Startup recovery, first half: a `running` row is work a crash interrupted."""
    interrupted = await scope.all(
        sa.select(m.Task).where(m.Task.status == m.TaskStatus.running)
    )
    for task in interrupted:
        task.status = m.TaskStatus.queued
        task.error = "interrupted — the service stopped while this ran"
        await scope.audit(
            "task_requeued",
            actor_kind="service",
            subject_kind="task",
            subject_id=task.id,
            payload={"kind": task.kind.value},
        )
    return len(interrupted)


async def rescue_accepted_bundles(scope: TenantScope) -> int:
    """Startup recovery, second half: an accepted bundle with no live task to apply it.

    This is also what un-sticks any bundle a pre-task-queue build left in ``accepted``
    — no data migration, the rescue is the migration.
    """
    live_applies = sa.select(m.Task.subject_id).where(
        m.Task.kind == m.TaskKind.apply_bundle, m.Task.status.in_(LIVE)
    )
    stranded = await scope.all(
        sa.select(m.Bundle).where(
            m.Bundle.status == m.BundleStatus.accepted,
            m.Bundle.id.not_in(live_applies),
        )
    )
    for bundle in stranded:
        await enqueue(
            scope,
            kind=m.TaskKind.apply_bundle,
            account_id=bundle.account_id,
            subject_id=bundle.id,
        )
    return len(stranded)


async def record_progress(
    service,  # noqa: ANN001 - Service; imported lazily to keep this module runner-free
    task_id: int,
    *,
    done: int,
    total: int | None,
    note: str | None,
) -> None:
    """One short transaction of its own — never called inside somebody else's."""
    async with service.scope() as scope:
        task = await scope.get(m.Task, task_id)
        if task is None or task.status is not m.TaskStatus.running:
            return
        task.progress_done = done
        task.progress_total = total
        task.progress_note = note
        await scope.commit()
