"""The bundle page's server half: its data, its two small changes, and what happens live.

The server decides everything and the browser displays it.  The interface itself is in
``frontend/``.  Accepting and rejecting stay ordinary forms.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from mailmind import tasks, views
from mailmind.db import models as m
from mailmind.suggest import model as suggest
from mailmind.suggest import staleness

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
    @app.get("/bundle/{bundle_id}", response_class=HTMLResponse)
    async def page(request: Request, bundle_id: int, error: str | None = None) -> Response:
        async with service.scope(readonly=True) as scope:
            if await scope.get(m.Bundle, bundle_id) is None:
                return HTMLResponse("no such bundle", status_code=404)
            return render(
                request,
                "bundle.html",
                bundle_id=bundle_id,
                error=error,
                script=script_for("src/bundle.js"),
                **await chrome(scope),
            )

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
                    task_id = await queue.get()
                    if task_id is None:
                        return
                    async with service.scope(readonly=True) as scope:
                        task = await scope.get(m.Task, task_id)
                        view = views.task_view(task) if task is not None else None
                    if view is not None:
                        yield f"event: task\ndata: {json.dumps(view)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")
