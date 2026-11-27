"""The server half of the interface built from ``frontend/``.

The browser holds one page for the whole review UI and routes between its views itself;
the server hands every one of those addresses the same shell, and decides everything the
views show, as data, with the changes they ask for and the stream that says what happened.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated

import sqlalchemy as sa
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from mailmind import tasks, views
from mailmind.db import models as m
from mailmind.suggest import model as suggest
from mailmind.suggest import staleness
from mailmind.worker import QueueChanged

#: What ``npm run build`` in frontend/ writes: index.html, and /_app beside it.
UI = Path(__file__).parent / "static" / "ui"

#: The addresses that are views of the one page.  A reload or a pasted link lands on any of
#: them, so each answers with the shell and the browser draws the view.
VIEWS = (
    "/",
    "/accounts",
    "/bundle/{bundle_id:int}",
    "/mail",
    "/mail/{container_id:int}",
    "/mail/message/{message_id:int}",
)

#: How many messages one folder page shows.  Not the agent's bounded-observation limit
#: (05): that one is about a model asking for more than it can read, and this is a person
#: paging through their own mail, told the total either way.
MAIL_PAGE = 100


def shell() -> tuple[str, list[str]] | None:
    """The built page, and the hashes of the inline script that boots it.

    Read per request: a checkout rebuilds under a running service.  SvelteKit starts from
    one inline script and writes its hash into a meta policy; the service's own header
    forbids inline script, so the hash has to be named there too, and only that hash.
    """
    try:
        page = (UI / "index.html").read_text()
    except FileNotFoundError:
        return None
    policy = re.search(r'http-equiv="content-security-policy" content="([^"]*)"', page)
    return page, re.findall(r"'sha256-[A-Za-z0-9+/=]+'", policy.group(1) if policy else "")


def _body(body: m.MessageBody) -> dict:
    return {
        "text": (body.text_plain or body.text_from_html or "")[:4000],
        "links": [
            {"text": link["text"], "target": link["target"]}
            for link in body.links.get("links", [])[:40]
        ],
    }


def mount(app, service, render, chrome, changes, reviewer, *, csrf: str, csp: str) -> None:  # noqa: ANN001
    async def view(request: Request) -> Response:
        built = shell()
        if built is None:
            async with service.scope(readonly=True) as scope:
                return render(
                    request,
                    "interface.html",
                    title="mailmind",
                    script=None,
                    data={},
                    **await chrome(scope),
                )
        page, hashes = built
        # The token the view's changes carry, in the page rather than in a request of its own.
        token = f'<meta name="mailmind-csrf" content="{csrf}">'
        response = HTMLResponse(page.replace("</head>", token + "</head>", 1))
        response.headers["Content-Security-Policy"] = csp.replace(
            "script-src 'self'", " ".join(["script-src 'self'", *hashes]), 1
        )
        return response

    for path in VIEWS:
        app.add_api_route(path, view, methods=["GET"], response_class=HTMLResponse)

    @app.get("/api/session")
    async def session() -> dict:
        """What every view's header shows: the accounts, which one is chosen, what waits."""
        async with service.scope(readonly=True) as scope:
            return await chrome(scope)

    @app.post("/api/account/choose", dependencies=changes)
    async def choose_account(account_id: Annotated[int, Form()]) -> dict:
        """Work in a different account.

        The choice belongs to the person rather than to the tenant, which is the shape it
        needs on a deployment where several authenticated people share one.
        """
        async with service.scope() as scope:
            account = await scope.get(m.Account, account_id)
            if account is None:
                raise HTTPException(404)
            (await reviewer(scope)).current_account_id = account.id
            await scope.commit()
            return await chrome(scope)

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

    @app.post("/api/queue/reject", dependencies=changes)
    async def reject(
        bundle: Annotated[list[int], Form()], reason: Annotated[str, Form()] = ""
    ) -> dict:
        """Several at once, because a rejection changes nothing in a mailbox.

        There is deliberately no accepting here: that is about what its page showed.
        """
        refused = []
        async with service.scope() as scope:
            person = await reviewer(scope)
            for bundle_id in dict.fromkeys(bundle):
                row = await scope.get(m.Bundle, bundle_id)
                try:
                    if row is None:
                        raise suggest.ProposalRefused("no such bundle")
                    await suggest.reject(scope, row, person, reason.strip() or None)
                except suggest.ProposalRefused as exc:
                    refused.append({"bundle_id": bundle_id, "problem": str(exc)})
            await scope.commit()
        return {"refused": refused}

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

    @app.post("/api/bundle/{bundle_id}/accept", dependencies=changes)
    async def accept(
        bundle_id: int,
        reviewed_through: Annotated[int, Form()] = 0,
        acknowledge_stale: Annotated[str, Form()] = "",
    ) -> dict:
        """Accept what the view showed, and hand the mailbox work to the runner.

        ``reviewed_through`` is what makes this "the bundle I read" rather than "this bundle
        as it stands now"; a refusal is an answer, not an error, because it is the view's
        job to show it where the person decided.
        """
        async with service.scope() as scope:
            bundle = await scope.get(m.Bundle, bundle_id)
            if bundle is None:
                raise HTTPException(404)
            person = await reviewer(scope)
            try:
                await suggest.accept(
                    scope,
                    bundle,
                    person,
                    reviewed_through=reviewed_through,
                    acknowledge_stale=bool(acknowledge_stale),
                )
            except suggest.ProposalRefused as exc:
                await scope.commit()
                return {"problem": str(exc)}
            # A failure is a task with a retry, not a stuck invisible bundle.
            await tasks.enqueue(
                scope,
                kind=m.TaskKind.apply_bundle,
                account_id=bundle.account_id,
                subject_id=bundle.id,
                requested_by=person.id,
            )
            await scope.commit()
        service.notify_tasks()
        return {"problem": None}

    @app.post("/api/bundle/{bundle_id}/reject", dependencies=changes)
    async def reject_one(bundle_id: int, reason: Annotated[str, Form()] = "") -> dict:
        async with service.scope() as scope:
            bundle = await scope.get(m.Bundle, bundle_id)
            if bundle is None:
                raise HTTPException(404)
            problem = None
            person = await reviewer(scope)
            try:
                await suggest.reject(scope, bundle, person, reason.strip() or None)
            except suggest.ProposalRefused as exc:
                problem = str(exc)
            await scope.commit()
        return {"problem": problem}

    # --------------------------------------------------- reading and filing by hand
    #
    # A person browsing their own mail, and filing it the same way an agent proposes:
    # picking messages builds a bundle whose producer is the reviewer, and the bundle's
    # view is where it is read and accepted.  Deliberately not a second way into the
    # mailbox: 01 says this is not where mail is read, and this stays a triage view rather
    # than a client: text only, no HTML, no attachments, nothing marked read.

    async def folder_options(scope, account_id: int) -> list[dict]:  # noqa: ANN001
        """Somewhere to move mail to, in the order the folder list shows it."""
        return views.flattened(
            views.as_tree(
                (await views.containers(scope, account_id))["containers"],
                await views.container_delimiter(scope, account_id),
            )
        )

    @app.get("/api/mail")
    async def mail() -> dict:
        async with service.scope(readonly=True) as scope:
            current = (await chrome(scope))["current_account"]
            tree = []
            if current:
                tree = views.as_tree(
                    (await views.containers(scope, current["id"]))["containers"],
                    await views.container_delimiter(scope, current["id"]),
                )
            return {"account": current, "tree": tree}

    @app.get("/api/mail/{container_id}")
    async def mail_container(container_id: int, offset: int = 0, unread: bool = False) -> dict:
        async with service.scope(readonly=True) as scope:
            container = await scope.get(m.Container, container_id)
            if container is None:
                raise HTTPException(404)
            offset = max(offset, 0)
            return {
                "container": {"id": container.id, "name": container.name},
                "listing": await views.messages(
                    scope,
                    container_id=container_id,
                    limit=MAIL_PAGE,
                    offset=offset,
                    unread_only=unread,
                ),
                "folders": await folder_options(scope, container.account_id),
                "offset": offset,
                "page": MAIL_PAGE,
            }

    @app.get("/api/mail/message/{message_id}")
    async def mail_message(message_id: int) -> dict:
        async with service.scope(readonly=True) as scope:
            try:
                detail = await views.message_detail(scope, message_id)
            except LookupError:
                raise HTTPException(404) from None
            container = (
                await scope.get(m.Container, detail["container_id"])
                if detail["container_id"]
                else None
            )
            if container is None:
                return {"message": detail, "container": None, "folders": []}
            return {
                "message": detail,
                "container": {"id": container.id, "name": container.name},
                "folders": await folder_options(scope, container.account_id),
            }

    @app.post("/api/mail/{container_id}/file", dependencies=changes)
    async def file_messages(
        container_id: int,
        message_id: Annotated[list[int], Form()] = (),
        operation: Annotated[str, Form()] = "move",
        target_container_id: Annotated[str, Form()] = "",
        target_container_name: Annotated[str, Form()] = "",
    ) -> dict:
        """What a person picked, proposed as a bundle of their own.

        Nothing here is a shorter path than the one an agent's proposal takes: the same
        enumerated effect is read, and the same accept applies it.
        """
        picked = list(dict.fromkeys(message_id))
        async with service.scope() as scope:
            container = await scope.get(m.Container, container_id)
            if container is None:
                raise HTTPException(404)
            account = await scope.get(m.Account, container.account_id)
            if not picked:
                return {"problem": "nothing was selected, so there is nothing to propose"}
            try:
                op = m.Operation(operation)
                if op not in (m.Operation.move, m.Operation.delete):
                    raise ValueError(operation)
            except ValueError:
                return {"problem": f"{operation!r} is not something this view proposes"}
            # A delete goes to Trash and names nothing else: one that carried a destination
            # would draw as "delete → Archive" on the bundle's view.
            named_id = target_container_id.strip() if op is m.Operation.move else ""
            named_name = target_container_name.strip() if op is m.Operation.move else ""
            if named_id and not named_id.isdigit():
                return {"problem": "that is not a folder this view offered"}
            try:
                bundle = await suggest.propose_bundle(
                    scope,
                    producer=await reviewer(scope),
                    account=account,
                    operation=op,
                    message_ids=picked,
                    summary=(
                        f"{len(picked)} message{'s' if len(picked) > 1 else ''} "
                        f"picked out of {container.name}"
                    ),
                    reason="Chosen by hand in the mail view.",
                    target_container_id=int(named_id) if named_id else None,
                    target_container_name=named_name or None,
                    expiry_days=service.config.limits.bundle_expiry_days,
                    max_size=service.config.limits.max_bundle_size,
                )
            except suggest.ProposalRefused as exc:
                # A refusal can come after the bundle row was written, so the half of it
                # that exists goes rather than being left in the queue for review.
                await scope.rollback()
                return {"problem": str(exc)}
            bundle_id = bundle.id
            await scope.commit()
        return {"bundle_id": bundle_id}

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
                    if isinstance(fact, QueueChanged):
                        changed = json.dumps({"bundles": fact.bundles})
                        yield f"event: queue\ndata: {changed}\n\n"
                        continue
                    async with service.scope(readonly=True) as scope:
                        task = await scope.get(m.Task, fact)
                        view = views.task_view(task) if task is not None else None
                    if view is not None:
                        yield f"event: task\ndata: {json.dumps(view)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")
