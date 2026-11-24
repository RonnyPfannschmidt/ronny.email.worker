"""The bundle page as a Svelte component: a spike, beside the page it may replace.

The server still decides everything.  What is here is the same data as JSON, the same two
changes behind the same CSRF dependency, and an event stream that carries facts about
tasks rather than HTML.  Accept and reject are the existing forms.
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


def mount(app, service, render, changes, reviewer) -> None:  # noqa: ANN001
    @app.get("/next/bundle/{bundle_id}", response_class=HTMLResponse)
    async def page(request: Request, bundle_id: int):  # noqa: ANN202
        return render(
            request, "next.html", bundle_id=bundle_id, script=script_for("src/bundle.js")
        )

    @app.get("/next/api/bundle/{bundle_id}")
    async def bundle(bundle_id: int) -> dict:
        async with service.scope() as scope:
            row = await scope.get(m.Bundle, bundle_id)
            if row is None:
                raise HTTPException(404)
            if row.status is m.BundleStatus.proposed:
                await staleness.refresh_bundle(scope, row)
                await scope.commit()
            detail = await views.bundle_detail(scope, bundle_id, limit=None)
            rows = detail.pop("items")["items"]
            ids = [row["message_id"] for row in rows]
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
                row["problem"] = ""
            return {**detail, "items": rows}

    @app.get("/next/api/message/{message_id}/body")
    async def message_body(message_id: int) -> dict:
        async with service.scope(readonly=True) as scope:
            body = await scope.scalar(
                sa.select(m.MessageBody).where(m.MessageBody.message_id == message_id)
            )
            if body is None:
                raise HTTPException(404)
            return _body(body)

    @app.post("/next/api/message/{message_id}/body", dependencies=changes)
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

    @app.post("/next/api/bundle/{bundle_id}/exclude/{suggestion_id}", dependencies=changes)
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

    @app.get("/next/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():  # noqa: ANN202
            hub = request.app.state.task_runner.hub
            queue = hub.subscribe()
            try:
                # Subscribed first, so what happens while the page reads is not missed.
                yield "event: ready\ndata: {}\n\n"
                while True:
                    task_id = await queue.get()
                    async with service.scope(readonly=True) as scope:
                        task = await scope.get(m.Task, task_id)
                        view = views.task_view(task) if task is not None else None
                    if view is not None:
                        yield f"event: task\ndata: {json.dumps(view)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")
