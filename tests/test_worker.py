"""The runner's behaviour: tasks actually happen, failures land in the row, work resumes.

Driven through :func:`mailmind.worker.execute` and :func:`run_all_pending` rather than a
sleeping dispatcher, so nothing here waits on a clock.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from mailmind import tasks, worker
from mailmind.config import AccountConfig, Config, Limits, Login
from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.imap import sync
from mailmind.imap.capabilities import probe_account
from mailmind.service import Service, hash_token
from mailmind.suggest import model as suggest
from tests.corpus import CORPUS


@pytest.fixture
async def service(tmp_path, backend):
    """A Service over a migrated database with the corpus synced — no HTTP anywhere."""
    for raw in CORPUS.values():
        backend.add_message("INBOX", raw)
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    upgrade_to_head(url)
    service = Service(
        Config(
            database_url=url,
            limits=Limits(max_messages_per_request=50),
            accounts=(
                AccountConfig(
                    name="test", host="h", login=Login(username="u", password="env://X")
                ),
            ),
        ),
        backend_factory=lambda _config: backend,
    )
    async with service.scope() as scope:
        account = scope.add(
            m.Account(name="test", host="h", username="u", password_url="env://X")
        )
        await scope.flush()
        for cap in ("CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE"):
            scope.add(m.AccountCapability(account_id=account.id, name=cap))
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        scope.add(m.Producer(kind=m.ProducerKind.person, name="reviewer"))
        await scope.flush()
        scope.add(
            m.Grant(
                producer_id=producer.id,
                token_hash=hash_token("t"),
                capabilities=["observe", "suggest"],
            )
        )
        await probe_account(scope, account, backend)
        for container in await sync.discover_containers(scope, account, backend):
            await sync.sync_container(scope, account, container, backend)
        await scope.commit()
    yield service
    await service.dispose()


async def _accepted_bundle(service, names=("newsletter",)) -> tuple[int, int]:
    """A bundle a person said yes to, with its apply task enqueued — returns ids."""
    async with service.scope() as scope:
        account = await scope.scalar(sa.select(m.Account))
        producer = await scope.scalar(
            sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.agent)
        )
        reviewer = await scope.scalar(
            sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.person)
        )
        archive = await scope.scalar(
            sa.select(m.Container).where(m.Container.name == "Archive")
        )
        wanted = await scope.all(
            sa.select(m.Message.id).order_by(m.Message.id).limit(len(names))
        )
        bundle = await suggest.propose_bundle(
            scope,
            producer=producer,
            account=account,
            operation=m.Operation.move,
            message_ids=list(wanted),
            target_container_id=archive.id,
            summary="s",
            reason="r",
        )
        await suggest.accept(
            scope,
            bundle,
            reviewer,
            reviewed_through=suggest.shown_through(bundle),
        )
        task, _ = await tasks.enqueue(
            scope,
            kind=m.TaskKind.apply_bundle,
            account_id=account.id,
            subject_id=bundle.id,
        )
        await scope.commit()
        return bundle.id, task.id


async def test_an_accepted_bundle_gets_applied_by_the_runner(service, backend):
    bundle_id, task_id = await _accepted_bundle(service)
    assert len(backend.folders["Archive"].messages) == 0

    assert await worker.run_all_pending(service) == 1

    assert len(backend.folders["Archive"].messages) == 1
    async with service.scope() as scope:
        bundle = await scope.get(m.Bundle, bundle_id)
        task = await scope.get(m.Task, task_id)
    assert bundle.status is m.BundleStatus.applied
    assert task.status is m.TaskStatus.done
    assert task.result["applied"] == 1


async def test_an_unreachable_mailbox_fails_the_task_and_a_retry_finishes_it(service, backend):
    """The stuck-accepted hole, closed: the failure is a visible row, and retrying works."""
    bundle_id, task_id = await _accepted_bundle(service)
    backend.force_unreachable()

    await worker.run_all_pending(service)

    async with service.scope() as scope:
        task = await scope.get(m.Task, task_id)
        bundle = await scope.get(m.Bundle, bundle_id)
        assert task.status is m.TaskStatus.failed
        assert task.error
        assert bundle.status is m.BundleStatus.accepted, "the accept still stands"

        # The mailbox comes back; a person presses retry.
        backend.reachable = True
        account = await scope.get(m.Account, bundle.account_id)
        account.health = m.AccountHealth.ok
        await tasks.enqueue(
            scope,
            kind=m.TaskKind.apply_bundle,
            account_id=bundle.account_id,
            subject_id=bundle_id,
        )
        await scope.commit()

    await worker.run_all_pending(service)
    async with service.scope() as scope:
        bundle = await scope.get(m.Bundle, bundle_id)
    assert bundle.status is m.BundleStatus.applied
    assert len(backend.folders["Archive"].messages) == 1


async def test_a_resumed_apply_reports_the_whole_truth(service, backend):
    """Items applied by an earlier run count in the rollup, not only this run's."""
    bundle_id, task_id = await _accepted_bundle(service, names=("newsletter", "ordinary"))
    async with service.scope() as scope:
        bundle = await scope.get(m.Bundle, bundle_id)
        # As if a previous run applied the first item and died before the second.
        first = sorted(bundle.suggestions, key=lambda s: s.id)[0]
        first.status = m.SuggestionStatus.applied
        await scope.commit()

    await worker.run_all_pending(service)

    async with service.scope() as scope:
        bundle = await scope.get(m.Bundle, bundle_id)
        task = await scope.get(m.Task, task_id)
    assert bundle.status is m.BundleStatus.applied
    assert task.result["applied"] == 2, "both runs' items, not one"
    assert len(backend.folders["Archive"].messages) == 1, "only this run's item moved"


async def test_crash_leftovers_converge_and_then_complete(service, backend):
    """`running` rows requeue, stranded accepted bundles get a task, and both finish."""
    bundle_id, task_id = await _accepted_bundle(service)
    async with service.scope() as scope:
        # The crash: the task was mid-run, and a second bundle never got its task.
        task = await scope.get(m.Task, task_id)
        task.status = m.TaskStatus.running
        assert await tasks.requeue_interrupted(scope) == 1
        assert await tasks.rescue_accepted_bundles(scope) == 0, "it has its task back"
        await scope.commit()

    await worker.run_all_pending(service)
    async with service.scope() as scope:
        bundle = await scope.get(m.Bundle, bundle_id)
    assert bundle.status is m.BundleStatus.applied


async def test_a_sync_task_fills_the_cache_and_reports_what_it_did(service, backend):
    backend.add_message("INBOX", b"Subject: fresh\r\nFrom: new@example.org\r\n\r\nhi")
    async with service.scope() as scope:
        account = await scope.scalar(sa.select(m.Account))
        task, _ = await tasks.enqueue(
            scope,
            kind=m.TaskKind.sync_account,
            account_id=account.id,
            subject_id=account.id,
        )
        await scope.commit()
        task_id = task.id

    await worker.run_all_pending(service)

    async with service.scope() as scope:
        task = await scope.get(m.Task, task_id)
        cached = await scope.scalar(sa.select(sa.func.count()).select_from(m.Message))
    assert task.status is m.TaskStatus.done
    assert task.result["added"] == 1
    assert cached == len(CORPUS) + 1


async def test_a_body_fetch_failure_is_a_row_somebody_can_read(service, backend):
    """The silent `contextlib.suppress` hole, closed: the error lands in the task."""
    async with service.scope() as scope:
        account = await scope.scalar(sa.select(m.Account))
        message = await scope.scalar(sa.select(m.Message).order_by(m.Message.id))
        task, _ = await tasks.enqueue(
            scope,
            kind=m.TaskKind.fetch_body,
            account_id=account.id,
            subject_id=message.id,
        )
        await scope.commit()
        task_id, message_id = task.id, message.id

    backend.force_unreachable()
    await worker.run_all_pending(service)
    async with service.scope() as scope:
        task = await scope.get(m.Task, task_id)
    assert task.status is m.TaskStatus.failed
    assert task.error

    backend.reachable = True
    async with service.scope() as scope:
        account = await scope.scalar(sa.select(m.Account))
        account.health = m.AccountHealth.ok
        await tasks.enqueue(
            scope,
            kind=m.TaskKind.fetch_body,
            account_id=account.id,
            subject_id=message_id,
        )
        await scope.commit()
    await worker.run_all_pending(service)
    async with service.scope() as scope:
        body = await scope.scalar(
            sa.select(m.MessageBody).where(m.MessageBody.message_id == message_id)
        )
    assert body is not None


async def test_the_runner_dispatches_for_real_and_stops_cleanly(service, backend):
    """The dispatcher end to end: recovery on start, wakeup on notify, lanes, shutdown."""
    import asyncio

    bundle_id, _ = await _accepted_bundle(service)

    runner = worker.TaskRunner(service, poll_interval=0.05)
    async with asyncio.TaskGroup() as tg:
        await runner.start(tg)
        service.notify_tasks()

        async def applied() -> None:
            while True:
                async with service.scope() as scope:
                    bundle = await scope.get(m.Bundle, bundle_id)
                    if bundle.status is m.BundleStatus.applied:
                        return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(applied(), timeout=10)
        await runner.stop()

    assert len(backend.folders["Archive"].messages) == 1


async def test_a_database_migrated_under_a_live_service_flips_it_to_not_operating(
    service, backend
):
    """The drift check: rather than `no such column` from inside a sync, every request
    starts answering 503 and the dispatcher stops claiming."""
    from mailmind.db.migrate import downgrade_to

    runner = worker.TaskRunner(service)
    await asyncio.to_thread(downgrade_to, service.config.database_url, "0006folder")

    await runner._tick_if_due()

    assert service.schema_problem is not None
    assert "0007task" in service.schema_problem
    assert "restart" in service.schema_problem
    assert runner._stopping.is_set(), "the dispatcher must stop claiming"


async def test_the_dispatcher_survives_a_locked_database(service, backend, monkeypatch):
    """The half-dead server bug: a claim colliding with a long write transaction raised
    `database is locked` out of the dispatcher, whose death cancelled the lifespan and
    tore the MCP session manager down while uvicorn kept serving.  A failed pass is a
    logged retry now, never an unwinding."""
    bundle_id, _ = await _accepted_bundle(service)

    real = tasks.claim_next
    calls = 0

    async def flaky(scope, *, busy_accounts):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sa.exc.OperationalError(
                "BEGIN IMMEDIATE", None, Exception("database is locked")
            )
        return await real(scope, busy_accounts=busy_accounts)

    monkeypatch.setattr(worker.tasks, "claim_next", flaky)

    runner = worker.TaskRunner(service, poll_interval=0.05)
    async with asyncio.TaskGroup() as tg:
        await runner.start(tg)
        service.notify_tasks()

        async def applied() -> None:
            while True:
                async with service.scope() as scope:
                    bundle = await scope.get(m.Bundle, bundle_id)
                    if bundle.status is m.BundleStatus.applied:
                        return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(applied(), timeout=10)
        await runner.stop()

    assert calls >= 2, "the first, failing pass was retried"
    assert len(backend.folders["Archive"].messages) == 1


async def test_a_bug_in_the_dispatcher_fails_the_whole_service_not_half(
    service, backend, monkeypatch
):
    """A database error retries; anything else must not linger as a half-dead app."""

    async def broken(scope, *, busy_accounts):
        raise ValueError("a genuine bug, not weather")

    monkeypatch.setattr(worker.tasks, "claim_next", broken)
    pulled_the_plug = asyncio.Event()
    monkeypatch.setattr(worker, "_shut_the_whole_service_down", pulled_the_plug.set)

    runner = worker.TaskRunner(service, poll_interval=0.05)
    async with asyncio.TaskGroup() as tg:
        await runner.start(tg)
        service.notify_tasks()
        await asyncio.wait_for(pulled_the_plug.wait(), timeout=10)
        await runner.stop()
