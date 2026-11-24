"""The server half of the interface built from ``frontend/``.

The server decides everything and the browser displays it: what the pages show is here as
data, with the changes their scripts ask for and the stream that says what happened.
Accepting and rejecting stay ordinary forms.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from mailmind import tasks, views
from mailmind.db import models as m
from mailmind.suggest import model as suggest
from mailmind.suggest import staleness
from mailmind.worker import QUEUE_CHANGED

ASSETS = Path(__file__).parent / "static" / "app"


#: Per entry point, the script Vite built for it.  Read per request: a checkout rebuilds
#: under a running service.
def script_for(entry: str) -> str | None:
    try:
        manifest = json.loads((ASSETS / "manifest.json").read_text())
    except FileNotFoundError:
        return None
    return f"/static/app/{manifest[entry]['file']}"


def _body(body: m.MessageBody) -> dict:
    return {
        "text": (body.text_plain or body.text_from_html or "")[:4000],
        "links": [
            {"text": link["text"], "target": link["target"]}
            for link in body.links.get("links", [])[:40]
        ],
    }


def mount(app, service, render, chrome, changes, reviewer) -> None:  # noqa: ANN001
    async def draw(request, scope, page: str, title: str, **data) -> HTMLResponse:  # noqa: ANN001
        return render(
            request,
            "interface.html",
            title=title,
            script=script_for(f"src/{page}.js"),
            data=data,
            **await chrome(scope),
        )

    @app.get("/", response_class=HTMLResponse)
    async def queue_page(request: Request) -> HTMLResponse:
        async with service.scope(readonly=True) as scope:
            return await draw(request, scope, "queue", "review queue")

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request) -> HTMLResponse:
        async with service.scope(readonly=True) as scope:
            return await draw(request, scope, "accounts", "accounts")

    @app.get("/bundle/{bundle_id}", response_class=HTMLResponse)
    async def bundle_page(request: Request, bundle_id: int, error: str = "") -> HTMLResponse:
        async with service.scope(readonly=True) as scope:
            if await scope.get(m.Bundle, bundle_id) is None:
                return HTMLResponse("no such bundle", status_code=404)
            return await draw(request, scope, "bundle", "review", bundle=bundle_id, error=error)

    @app.get("/api/queue")
    async def queue() -> dict:
        async with service.scope(readonly=True) as scope:
            header = await chrome(scope)
            current = header["current_account"]
            # None means every account, which is only reachable when there are none at all.
            here = {current["id"]} if current else None

            async def listed(*statuses, limit: int | None = None) -> list[dict]:  # noqa: ANN002
                found = await views.bundle_summaries(
                    scope, list(statuses), account_ids=here, limit=limit
                )
                return found["bundles"]

            status = m.BundleStatus
            applying = await listed(status.accepted)
            applies: dict = {}
            for task in await views.task_summaries(
                scope, here, kinds=[m.TaskKind.apply_bundle]
            ):
                applies.setdefault(task["subject_id"], task)
            # Expiry and staleness are the runner's, once a minute: this scope cannot write.
            return {
                "account": current["name"] if current else None,
                "elsewhere": [
                    {"name": a["name"], "waiting": header["waiting"][a["id"]]}
                    for a in header["accounts"]
                    if current and a["id"] != current["id"] and header["waiting"].get(a["id"])
                ],
                "bundles": await listed(status.proposed),
                "applying": [{**b, "task": applies.get(b["bundle_id"])} for b in applying],
                "recent": await listed(
                    status.applied,
                    status.partially_applied,
                    status.rejected,
                    status.expired,
                    status.withdrawn,
                    status.stale,
                    limit=10,
                ),
            }

    @app.get("/api/accounts")
    async def accounts() -> dict:
        async with service.scope(readonly=True) as scope:
            syncs: dict = {}
            for task in await views.task_summaries(
                scope, None, kinds=[m.TaskKind.sync_account]
            ):
                syncs.setdefault(task["account_id"], task)
            found = []
            for account in await scope.all(sa.select(m.Account).order_by(m.Account.id)):
                capabilities = await scope.all(
                    sa.select(m.AccountCapability)
                    .where(m.AccountCapability.account_id == account.id)
                    .order_by(m.AccountCapability.name)
                )
                found.append(
                    {
                        "id": account.id,
                        "name": account.name,
                        "health": account.health.value,
                        "health_detail": account.health_detail,
                        "capabilities": [
                            {
                                "name": c.name,
                                "declared": c.declared,
                                "offered": c.probed_present,
                            }
                            for c in capabilities
                        ],
                        "containers": (await views.containers(scope, account.id))["containers"],
                        "task": syncs.get(account.id),
                    }
                )
            return {"accounts": found}

    @app.post("/api/account/{account_id}/sync", dependencies=changes)
    async def sync_account(account_id: int) -> dict:
        async with service.scope() as scope:
            account = await scope.get(m.Account, account_id)
            if account is None:
                raise HTTPException(404)
            task, _ = await tasks.enqueue(
                scope,
                kind=m.TaskKind.sync_account,
                account_id=account.id,
                subject_id=account.id,
                requested_by=(await reviewer(scope)).id,
            )
            await scope.commit()
            view = views.task_view(task)
        service.notify_tasks()
        return view

    @app.post("/api/task/{task_id}/retry", dependencies=changes)
    async def retry(task_id: int) -> dict:
        """Ask again.  Only a failed task; coalescing makes double-clicks harmless."""
        async with service.scope() as scope:
            task = await scope.get(m.Task, task_id)
            if task is None:
                raise HTTPException(404)
            if task.status is not m.TaskStatus.failed:
                return views.task_view(task)
            if task.kind is m.TaskKind.apply_bundle:
                bundle = await scope.get(m.Bundle, task.subject_id)
                if bundle is None or bundle.status is not m.BundleStatus.accepted:
                    return views.task_view(task)
            again, _ = await tasks.enqueue(
                scope,
                kind=task.kind,
                account_id=task.account_id,
                subject_id=task.subject_id,
                payload=dict(task.payload),
                requested_by=(await reviewer(scope)).id,
            )
            await scope.commit()
            view = views.task_view(again)
        service.notify_tasks()
        return view

    @app.get("/api/bundle/{bundle_id}")
    async def bundle(bundle_id: int) -> dict:
        async with service.scope() as scope:
            row = await scope.get(m.Bundle, bundle_id)
            if row is None:
                raise HTTPException(404)
            if row.status is m.BundleStatus.proposed:
                # The first of the two staleness checks: before it is shown.
                await staleness.refresh_bundle(scope, row)
                await scope.commit()
            # limit=None: a reviewer is shown the whole of what they are accepting, and
            # `reviewed_through` is only honest about a page that held all of it.
            detail = await views.bundle_detail(scope, bundle_id, limit=None)
            rows = detail.pop("items")["items"]
            ids = [row["message_id"] for row in rows if row["message_id"] is not None]
            bodies = {
                body.message_id: _body(body)
                for body in await scope.all(
                    sa.select(m.MessageBody).where(m.MessageBody.message_id.in_(ids))
                )
            }
            fetches: dict = {}
            for task in await views.task_summaries(scope, None, kinds=[m.TaskKind.fetch_body]):
                fetches.setdefault(task["subject_id"], task)
            for row in rows:
                row["findings"] = [f for a in row.pop("assessment", []) for f in a["findings"]]
                row["body"] = bodies.get(row["message_id"])
                row["task"] = fetches.get(row["message_id"])
            applies = await views.task_summaries(scope, None, kinds=[m.TaskKind.apply_bundle])
            apply_task = next((t for t in applies if t["subject_id"] == bundle_id), None)
            return {**detail, "items": rows, "apply_task": apply_task}

    @app.get("/api/message/{message_id}")
    async def message(message_id: int) -> dict:
        async with service.scope(readonly=True) as scope:
            try:
                detail = await views.message_detail(scope, message_id, include_body=True)
            except LookupError:
                raise HTTPException(404) from None
            fetches = await views.task_summaries(scope, None, kinds=[m.TaskKind.fetch_body])
            task = next((t for t in fetches if t["subject_id"] == message_id), None)
            body = detail["body"]
            return {
                "task": task,
                "body": body
                and {
                    "text": body["text"],
                    "links": body["links"]["links"],
                    "attachments": [
                        a["filename"] or a["content_type"]
                        for a in body["attachments"]["attachments"]
                    ],
                },
            }

    @app.get("/api/message/{message_id}/body")
    async def message_body(message_id: int) -> dict:
        async with service.scope(readonly=True) as scope:
            body = await scope.scalar(
                sa.select(m.MessageBody).where(m.MessageBody.message_id == message_id)
            )
            if body is None:
                raise HTTPException(404)
            return _body(body)

    @app.post("/api/message/{message_id}/body", dependencies=changes)
    async def fetch_body(message_id: int) -> dict:
        async with service.scope() as scope:
            placement = await scope.scalar(
                views.live_placements().where(m.Placement.message_id == message_id)
            )
            if placement is None:
                raise HTTPException(404)
            container = await scope.get(m.Container, placement.container_id)
            task, _ = await tasks.enqueue(
                scope,
                kind=m.TaskKind.fetch_body,
                account_id=container.account_id,
                subject_id=message_id,
                requested_by=(await reviewer(scope)).id,
            )
            await scope.commit()
            view = views.task_view(task)
        service.notify_tasks()
        return view

    @app.post("/api/bundle/{bundle_id}/exclude/{suggestion_id}", dependencies=changes)
    async def exclude(bundle_id: int, suggestion_id: int) -> dict:
        async with service.scope() as scope:
            suggestion = await scope.get(m.Suggestion, suggestion_id)
            if suggestion is None or suggestion.bundle_id != bundle_id:
                raise HTTPException(404)
            problem = ""
            try:
                await suggest.exclude(scope, suggestion, await reviewer(scope))
            except suggest.ProposalRefused as exc:
                problem = str(exc)
            await scope.commit()
            return {"status": suggestion.status.value, "problem": problem}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():  # noqa: ANN202
            hub = request.app.state.task_runner.hub
            queue = hub.subscribe()
            try:
                # Subscribed first, so what happens while the page reads is not missed.
                yield "event: ready\ndata: {}\n\n"
                while True:
                    fact = await queue.get()
                    if fact is None:
                        return
                    if fact == QUEUE_CHANGED:
                        yield "event: queue\ndata: {}\n\n"
                        continue
                    async with service.scope(readonly=True) as scope:
                        task = await scope.get(m.Task, fact)
                        view = views.task_view(task) if task is not None else None
                    if view is not None:
                        yield f"event: task\ndata: {json.dumps(view)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")
