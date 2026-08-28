"""The task runner: one dispatcher on the loop, one lane per account.

The data half lives in :mod:`mailmind.tasks`; this is the half that makes rows happen.
A single dispatcher claims the oldest queued task whose account has no lane running and
gives it a lane coroutine; different accounts run in parallel, one task per account at a
time.  Executors own their transactions on the loop and dip to threads only for IMAP —
the same shape as everything else since the async conversion.

An executor failure marks its task ``failed`` and never escapes: the runner's TaskGroup
carries the app's lifespan, and a bug in one sync must not take the service down.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal
import traceback

import sqlalchemy as sa

from mailmind import tasks, views
from mailmind.db import models as m
from mailmind.db.migrate import schema_problem
from mailmind.imap import apply as applier
from mailmind.imap import sync
from mailmind.imap.backend import TRASH, MailboxUnhealthy
from mailmind.service import Service
from mailmind.suggest import model as suggest
from mailmind.suggest import staleness

INTERRUPTED = "interrupted by shutdown"

log = logging.getLogger("mailmind.worker")


def _shut_the_whole_service_down() -> None:
    """Fail whole, never half.

    A bug escaping the dispatcher used to cancel the lifespan, which killed the MCP
    session manager while uvicorn kept serving pages — the worst state, because it looks
    alive.  A graceful SIGTERM takes everything down together: lanes requeue their work,
    the exit is visible, and a supervisor restarts it.  (Seam for tests.)
    """
    signal.raise_signal(signal.SIGTERM)


class Hub:
    """Task ids fan out to whoever is streaming; everything shares the loop."""

    def __init__(self) -> None:
        self._streams: set[asyncio.Queue[int]] = set()

    def publish(self, task_id: int) -> None:
        for stream in self._streams:
            stream.put_nowait(task_id)

    def subscribe(self) -> asyncio.Queue[int]:
        stream: asyncio.Queue[int] = asyncio.Queue()
        self._streams.add(stream)
        return stream

    def unsubscribe(self, stream: asyncio.Queue[int]) -> None:
        self._streams.discard(stream)


class TaskProgress:
    """A :class:`~mailmind.imap.sync.SyncProgress` that lands in the task row.

    The protocol's callbacks are sync and run on the loop between dips, so each update
    schedules (at most one in-flight) short transaction of its own — never inside the
    sync's transaction — and tells the hub.
    """

    def __init__(self, service: Service, task_id: int, hub: Hub, total: int | None) -> None:
        self._service = service
        self._task_id = task_id
        self._hub = hub
        self._done = 0
        self._total = total
        self._note: str | None = None
        self._writer: asyncio.Task | None = None

    def folder_started(self, container: str, messages: int) -> None:
        self._note = container
        self._flush()

    def messages_absorbed(self, count: int) -> None:
        self._done += count
        self._flush()

    def folder_finished(self, report: sync.SyncReport) -> None:
        self._flush()

    def _flush(self) -> None:
        if self._writer is not None and not self._writer.done():
            return  # one write in flight is throttle enough
        self._writer = asyncio.get_running_loop().create_task(self._write())

    async def _write(self) -> None:
        await tasks.record_progress(
            self._service,
            self._task_id,
            done=self._done,
            total=self._total,
            note=self._note,
        )
        self._hub.publish(self._task_id)

    async def settle(self) -> None:
        """Wait out the in-flight write, so `finish` is the last word on the row."""
        if self._writer is not None:
            await self._writer


class TaskRunner:
    def __init__(
        self,
        service: Service,
        *,
        poll_interval: float = 2.0,
        tick_interval: float = 60.0,
    ) -> None:
        self.service = service
        self.hub = Hub()
        self.poll_interval = poll_interval
        self.tick_interval = tick_interval
        self._wakeup = asyncio.Event()
        self._stopping = asyncio.Event()
        self._lanes: dict[int, asyncio.Task] = {}
        self._last_tick = 0.0

    async def start(self, tg: asyncio.TaskGroup) -> None:
        self.service.notify_tasks = self._wakeup.set
        try:
            async with self.service.scope() as scope:
                requeued = await tasks.requeue_interrupted(scope)
                rescued = await tasks.rescue_accepted_bundles(scope)
                await scope.commit()
            if requeued or rescued:
                self._wakeup.set()
        except Exception:
            # A busy database at startup is not a reason to have no service; the first
            # dispatch pass claims whatever this would have queued anyway.
            log.exception("startup recovery failed; the dispatcher will retry")
        self._tg = tg
        self._dispatching = tg.create_task(self._dispatch())

    async def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        await self._dispatching
        if self._lanes:
            await asyncio.gather(*self._lanes.values(), return_exceptions=True)

    async def _dispatch(self) -> None:
        """The loop that must not die.

        It runs as a child of the lifespan's TaskGroup, so an exception escaping here
        cancels the lifespan — which tears the MCP session manager down while uvicorn
        keeps serving, and the service is half-dead until a restart.  That happened: a
        `database is locked` from a claim colliding with a long sync transaction took
        the whole background system with it.  Two failures, two answers: a database
        error is expected weather under SQLite — one writer, long transactions — and is
        a logged retry; anything else is a bug, and a bug shuts the whole service down
        rather than half of it.
        """
        while not self._stopping.is_set():
            try:
                claimed = await self._one_pass()
            except sa.exc.OperationalError as exc:
                # The claim lost a busy_timeout race with a long write (a first sync of
                # a big folder, most likely).  Transient by construction: retry.
                log.warning(
                    "dispatch pass failed on the database (%s); retrying in %ss",
                    exc,
                    self.poll_interval,
                )
                claimed = None
            except Exception:
                log.critical(
                    "dispatch pass hit a bug; shutting the service down whole", exc_info=True
                )
                _shut_the_whole_service_down()
                return
            if claimed:
                continue
            try:
                async with asyncio.timeout(self.poll_interval):
                    await self._wakeup.wait()
            except TimeoutError:
                pass
            self._wakeup.clear()

    async def _one_pass(self) -> bool:
        """Housekeeping if due, then claim at most one task.  True if one was claimed."""
        await self._tick_if_due()
        async with self.service.scope() as scope:
            claimed = await tasks.claim_next(scope, busy_accounts=set(self._lanes))
            if claimed is None:
                return False
            task_id, account_id = claimed.id, claimed.account_id
            await scope.commit()
        self.hub.publish(task_id)
        self._lanes[account_id] = self._tg.create_task(self._run_one(task_id, account_id))
        return True

    async def _run_one(self, task_id: int, account_id: int) -> None:
        try:
            await execute(self.service, task_id, self._stopping, hub=self.hub)
        finally:
            self._lanes.pop(account_id, None)
            self._wakeup.set()

    async def _tick_if_due(self) -> None:
        """Housekeeping, off the request path — and the schema drift check first.

        Expiry and the staleness sweep used to run in the queue GET, which made every
        page load a writer.  The drift check catches a database migrated under a live
        process: rather than `no such column` from inside a sync, every request starts
        answering 503 with what happened, the dispatcher stops claiming, and a restart
        is the way back.
        """
        now = asyncio.get_running_loop().time()
        if now - self._last_tick < self.tick_interval:
            return
        self._last_tick = now
        problem = await asyncio.to_thread(
            schema_problem, self.service.config.database_url
        )
        if problem is not None:
            self.service.schema_problem = (
                problem + " — the database changed under this running service; "
                "restart it once the migration is done"
            )
            self._stopping.set()
            return
        async with self.service.scope() as scope:
            await suggest.expire_due(scope)
            await staleness.sweep_queue(scope, None)
            await scope.commit()


async def execute(
    service: Service,
    task_id: int,
    stopping: asyncio.Event,
    *,
    hub: Hub | None = None,
) -> None:
    """One task, whatever becomes of it.  The seam the tests drive directly.

    Never raises: an executor bug is that task's failure, not the service's.
    """
    hub = hub or Hub()
    try:
        async with service.scope() as scope:
            task = await scope.get(m.Task, task_id)
            if task is None or task.status is not m.TaskStatus.running:
                return
            executor = {
                m.TaskKind.apply_bundle: _execute_apply,
                m.TaskKind.sync_account: _execute_sync_account,
                m.TaskKind.sync_container: _execute_sync_container,
                m.TaskKind.fetch_body: _execute_fetch_body,
            }[task.kind]
            await executor(service, scope, task, stopping, hub)
            await scope.commit()
    except Exception:
        # The last resort: whatever broke, the row says so and the runner lives.
        try:
            async with service.scope() as scope:
                task = await scope.get(m.Task, task_id)
                if task is not None and task.status is m.TaskStatus.running:
                    await tasks.finish(
                        scope,
                        task,
                        m.TaskStatus.failed,
                        error=traceback.format_exc(limit=8),
                    )
                    await scope.commit()
        except Exception:  # noqa: BLE001 - nothing left to report to
            pass
    finally:
        hub.publish(task_id)


async def run_all_pending(service: Service, *, limit: int = 100) -> int:
    """Claim-and-execute until the queue is dry.  What tests call instead of sleeping."""
    stopping = asyncio.Event()
    ran = 0
    for _ in range(limit):
        async with service.scope() as scope:
            claimed = await tasks.claim_next(scope, busy_accounts=set())
            if claimed is None:
                return ran
            task_id = claimed.id
            await scope.commit()
        await execute(service, task_id, stopping)
        ran += 1
    return ran


async def _requeue(scope, task) -> None:  # noqa: ANN001
    task.status = m.TaskStatus.queued
    task.error = INTERRUPTED


async def _execute_apply(service, scope, task, stopping, hub) -> None:  # noqa: ANN001
    bundle = await scope.get(m.Bundle, task.subject_id)
    if bundle is None or bundle.status is not m.BundleStatus.accepted:
        await tasks.finish(
            scope,
            task,
            m.TaskStatus.done,
            result={"note": "nothing to apply — the bundle is not accepted"},
        )
        return
    account = await scope.get(m.Account, bundle.account_id)
    trash = await scope.scalar(
        sa.select(m.Container).where(
            m.Container.account_id == account.id, m.Container.special_use == TRASH
        )
    )

    def on_item(done: int, total: int) -> None:
        task.progress_done = done
        task.progress_total = total
        hub.publish(task.id)

    try:
        async with service.backend(account) as backend:
            await applier.apply_bundle(
                scope,
                bundle,
                backend,
                trash_container=trash,
                checkpoint=scope.commit,
                should_stop=stopping.is_set,
                on_item=on_item,
            )
    except applier.NotApplicable as exc:
        if bundle.status is m.BundleStatus.stale:
            # Not a failure of the worker: the mail moved on and the bundle closed as
            # what became of it.
            await tasks.finish(scope, task, m.TaskStatus.done, result={"note": str(exc)})
        else:
            await tasks.finish(scope, task, m.TaskStatus.failed, error=str(exc))
        return
    except MailboxUnhealthy as exc:
        _note_unhealthy(account, exc)
        await tasks.finish(scope, task, m.TaskStatus.failed, error=str(exc))
        return
    if bundle.status is m.BundleStatus.accepted:
        # should_stop cut the run short; what finished is committed, the rest waits.
        await _requeue(scope, task)
        return
    counts = {
        "applied": sum(1 for s in bundle.suggestions if s.status is m.SuggestionStatus.applied),
        "status": bundle.status.value,
    }
    await tasks.finish(scope, task, m.TaskStatus.done, result=counts)


async def _execute_sync_account(service, scope, task, stopping, hub) -> None:  # noqa: ANN001
    account = await scope.get(m.Account, task.subject_id)
    if account is None:
        await tasks.finish(
            scope, task, m.TaskStatus.done, result={"note": "the account is gone"}
        )
        return
    try:
        async with service.backend(account) as backend:
            folders = [
                c
                for c in await sync.discover_containers(scope, account, backend)
                if c.selectable
            ]
            expected = await asyncio.to_thread(
                sync.messages_to_read, folders, backend, force_full=False
            )
            progress = TaskProgress(service, task.id, hub, expected)
            reports = await sync.sync_account(
                scope,
                account,
                backend,
                progress=progress,
                should_stop=stopping.is_set,
            )
            await progress.settle()
    except MailboxUnhealthy as exc:
        _note_unhealthy(account, exc)
        await tasks.finish(scope, task, m.TaskStatus.failed, error=str(exc))
        return
    if stopping.is_set() and len(reports) < len(folders):
        await _requeue(scope, task)
        return
    await tasks.finish(scope, task, m.TaskStatus.done, result=_rollup(reports))


async def _execute_sync_container(service, scope, task, stopping, hub) -> None:  # noqa: ANN001
    container = await scope.get(m.Container, task.subject_id)
    if container is None:
        await tasks.finish(
            scope, task, m.TaskStatus.done, result={"note": "the folder is gone"}
        )
        return
    account = await scope.get(m.Account, container.account_id)
    try:
        async with service.backend(account) as backend:
            report = await sync.sync_container(scope, account, container, backend)
    except MailboxUnhealthy as exc:
        _note_unhealthy(account, exc)
        await tasks.finish(scope, task, m.TaskStatus.failed, error=str(exc))
        return
    await tasks.finish(scope, task, m.TaskStatus.done, result=_rollup([report]))


async def _execute_fetch_body(service, scope, task, stopping, hub) -> None:  # noqa: ANN001
    placement = await scope.scalar(
        views.live_placements().where(m.Placement.message_id == task.subject_id)
    )
    if placement is None:
        await tasks.finish(
            scope,
            task,
            m.TaskStatus.done,
            result={"note": "the message has moved on; there is nothing to fetch"},
        )
        return
    container = await scope.get(m.Container, placement.container_id)
    account = await scope.get(m.Account, container.account_id)
    try:
        async with service.backend(account) as backend:
            await sync.fetch_and_cache_body(
                scope,
                account,
                container,
                placement,
                backend,
                budget_bytes=service.config.limits.body_cache_bytes,
            )
    except MailboxUnhealthy as exc:
        _note_unhealthy(account, exc)
        await tasks.finish(scope, task, m.TaskStatus.failed, error=str(exc))
        return
    await tasks.finish(scope, task, m.TaskStatus.done)


def _note_unhealthy(account: m.Account, exc: Exception) -> None:
    """The accounts page renders health, so a dead mailbox says so where it is seen."""
    account.health = m.AccountHealth.down
    account.health_detail = str(exc)
    account.health_checked_at = dt.datetime.now(dt.UTC)


def _rollup(reports: list[sync.SyncReport]) -> dict:
    return {
        "folders": len(reports),
        "added": sum(r.added for r in reports),
        "updated": sum(r.updated for r in reports),
        "vanished": sum(r.vanished for r in reports),
        "identity_broken": [r.container for r in reports if r.identity_broken],
    }
