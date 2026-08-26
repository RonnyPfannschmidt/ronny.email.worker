"""The task table's own behaviour: coalescing, claiming, recovery.

Pure data-layer tests — the runner that executes tasks has tests of its own.
"""

from __future__ import annotations

import sqlalchemy as sa

from mailmind import tasks
from mailmind.db import models as m


async def _sync_task(scope, world, **kw):
    defaults = dict(
        kind=m.TaskKind.sync_account,
        account_id=world["account"].id,
        subject_id=world["account"].id,
    )
    defaults.update(kw)
    return await tasks.enqueue(scope, **defaults)


async def test_asking_twice_is_the_same_ask(scope, world):
    """A second "sync now" while one waits must not stack a second sync."""
    first, created = await _sync_task(scope, world)
    again, created_again = await _sync_task(scope, world)
    assert created and not created_again
    assert again.id == first.id

    # Running still coalesces; finished does not.
    first.status = m.TaskStatus.running
    await scope.flush()
    still, created_third = await _sync_task(scope, world)
    assert still.id == first.id and not created_third

    await tasks.finish(scope, first, m.TaskStatus.done)
    fresh, created_fresh = await _sync_task(scope, world)
    assert created_fresh and fresh.id != first.id


async def test_claiming_is_fifo_and_respects_busy_lanes(scope, world):
    account = world["account"]
    other = scope.add(m.Account(name="other", host="h2", username="u2", password_url="env://Y"))
    await scope.flush()

    first, _ = await _sync_task(scope, world)
    second, _ = await _sync_task(scope, world, kind=m.TaskKind.apply_bundle, subject_id=999)
    elsewhere, _ = await _sync_task(scope, world, account_id=other.id, subject_id=other.id)

    claimed = await tasks.claim_next(scope, busy_accounts=set())
    assert claimed.id == first.id, "oldest first"
    assert claimed.status is m.TaskStatus.running
    assert claimed.attempts == 1

    # The first task's account now has a lane running: its queue-mate must wait, the
    # other account's task must not.
    next_up = await tasks.claim_next(scope, busy_accounts={account.id})
    assert next_up.id == elsewhere.id

    nothing = await tasks.claim_next(scope, busy_accounts={account.id, other.id})
    assert nothing is None


async def test_interrupted_work_requeues_and_stranded_bundles_get_a_task(scope, world):
    """A crash leaves `running` rows and `accepted` bundles; startup converges both."""
    running, _ = await _sync_task(scope, world)
    running.status = m.TaskStatus.running
    bundle = scope.add(
        m.Bundle(
            account_id=world["account"].id,
            producer_id=world["producer"].id,
            action_kind=m.ActionKind.state,
            operation=m.Operation.move,
            target_container_id=world["containers"]["Archive"].id,
            status=m.BundleStatus.accepted,
            summary="s",
            reason="r",
            expires_at=sa.func.now(),
        )
    )
    await scope.flush()

    assert await tasks.requeue_interrupted(scope) == 1
    assert running.status is m.TaskStatus.queued
    assert "interrupted" in running.error

    assert await tasks.rescue_accepted_bundles(scope) == 1
    rescue = await scope.scalar(
        sa.select(m.Task).where(
            m.Task.kind == m.TaskKind.apply_bundle, m.Task.subject_id == bundle.id
        )
    )
    assert rescue is not None and rescue.status is m.TaskStatus.queued

    # Rescuing again finds nothing stranded — the bundle has its task now.
    assert await tasks.rescue_accepted_bundles(scope) == 0
    applies = await scope.all(sa.select(m.Task).where(m.Task.kind == m.TaskKind.apply_bundle))
    assert len(applies) == 1


async def test_every_transition_leaves_a_mark(scope, world):
    task, _ = await _sync_task(scope, world)
    claimed = await tasks.claim_next(scope, busy_accounts=set())
    await tasks.finish(scope, claimed, m.TaskStatus.failed, error="boom")
    await scope.commit()

    verbs = [
        e.verb
        for e in await scope.all(
            sa.select(m.AuditEvent)
            .where(m.AuditEvent.subject_kind == "task")
            .order_by(m.AuditEvent.seq)
        )
    ]
    assert verbs == ["task_enqueued", "task_started", "task_finished"]
