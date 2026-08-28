"""One process, two surfaces.

``/mcp`` is where agents connect, authenticated by a bearer token that resolves to a
grant.  Everything else is the review UI, which is where acceptance lives.  They share a
database and nothing else: no route under ``/mcp`` can reach the applier, and the review
routes never consult a grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware

from mailmind import tasks, views
from mailmind.config import check_exposure, is_wildcard, oauth_issuer
from mailmind.db import models as m
from mailmind.mcp import oauth
from mailmind.mcp import server as mcp_server
from mailmind.service import Service
from mailmind.suggest import model as suggest
from mailmind.suggest import staleness
from mailmind.worker import TaskRunner

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _local(value, fmt: str):  # noqa: ANN001
    """An ISO timestamp in the machine's own time, or the value untouched if it is not one.

    A loopback tool runs where the person sits, so server-local time is their time."""
    if not value:
        return value
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone().strftime(fmt)


TEMPLATES.env.filters["when"] = lambda value: _local(value, "%d %b %Y %H:%M")
TEMPLATES.env.filters["day"] = lambda value: _local(value, "%d %b %Y")

#: No inline script, no eval, no remote anything.  A remote image would tell a sender
#: their mail was read, and this page renders mail written by strangers.  The one script
#: is the vendored Turbo served by this process from /static; connect-src covers its
#: form submissions and the SSE stream, both to ourselves.
CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; img-src 'none'"
)


async def chosen_account(scope) -> m.Account | None:  # noqa: ANN001
    """Which account the review UI is working in — a view, not a boundary
    (docs/design/11 has the argument).

    Nothing chosen falls back to the first account by name, so a fresh install has one
    without anybody having to pick.
    """
    person = await scope.scalar(
        sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.person)
    )
    if person is not None and person.current_account_id is not None:
        chosen = await scope.get(m.Account, person.current_account_id)
        if chosen is not None:
            return chosen
    return await scope.scalar(sa.select(m.Account).order_by(m.Account.name))


def unavailable_page(reason: str) -> HTMLResponse:
    """What every request gets while the database and the code disagree.

    Served without the session key on purpose: it holds no mail, only the fact that the
    service cannot operate and the command that fixes it — and it has to be reachable to
    say so, which is the whole point of not simply crashing.
    """
    return HTMLResponse(
        "<h1>Not operating</h1><p>" + reason + "</p><p>Nothing has been touched. "
        "Run <code>mailmindctl migrate</code> (with the service stopped, if one is "
        "running) and start this again.</p>",
        status_code=503,
    )


def create_unavailable_app(reason: str) -> FastAPI:
    """The app `serve` binds when the schema is behind: one answer, every path.

    No database, no runner, no MCP — a supervisor restarting into a behind checkout gets
    a stable page that explains itself instead of a crash loop nobody can see.
    """
    app = FastAPI(title="mailmind (not operating)")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    async def everything(path: str):  # noqa: ANN202, ARG001
        return unavailable_page(reason)

    return app


def csrf_token(session_key: str) -> str:
    """The per-session CSRF token, derived from the key so there is nothing new to store."""
    return hmac.new(session_key.encode(), b"csrf", hashlib.sha256).hexdigest()


def not_same_origin(request: Request) -> str | None:
    """Why this POST is not a same-origin request from a page this service served.

    The session cookie is the authentication; this is the CSRF half of it.
    ``Sec-Fetch-Site`` is a forbidden header — a browser sets it and script cannot — so
    ``same-origin`` here means a page this service served made the request, whether by a
    form submission or by fetch.  A browser too old to send fetch metadata must show a
    matching ``Origin`` instead.  See docs/security-model.md.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site != "same-origin":
            return f"sec-fetch-site was {site!r}, not 'same-origin'"
        return None
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    expected_origin = f"{request.url.scheme}://{host}"
    if origin != expected_origin:
        return f"origin was {origin!r}, not {expected_origin!r}, and no fetch metadata"
    return None


class CsrfRefused(Exception):
    """A change arrived without the form token every page carries."""


#: A checkbox group arrives as a repeated form field, which FastAPI reads into a list.
#: Built once at module level because a call in an argument default is a trap when the
#: default is mutable — these are the only two multi-valued fields in the UI.
TICKED_CAPABILITIES = Form(default=[])
TICKED_ACCOUNTS = Form(default=[])


#: The endpoints a machine talks to, which the review UI's key does not guard.
#:
#: Every one of them is part of a client logging in, and a client has no key and is not
#: supposed to: that is the whole point of the flow.  What they are guarded by instead is
#: the protocol — a registration is only a registration, a code is redeemable once and only
#: by whoever proved they asked for it.
#:
#: ``/consent`` is deliberately *not* here.  It is the one page in the flow where a person
#: decides something, so it is guarded like every other page a person uses.  The test that
#: says so is the point of writing this as a list rather than a condition.
MACHINE_PATHS = frozenset(
    {
        "/authorize",
        "/token",
        "/register",
        "/revoke",
    }
)


def is_machine_path(path: str) -> bool:
    """Whether this is something a client talks to rather than a person."""
    return path in MACHINE_PATHS or path.startswith("/mcp") or path.startswith("/.well-known/")


#: The cookie the review UI is reached with. Session-scoped, so it goes when the browser
#: does, and HttpOnly, so a page cannot read it back out — not that any page here runs
#: script, but a review UI renders mail written by strangers and the habit is cheap.
SESSION_COOKIE = "mailmind_session"

#: The query parameter that trades a key for that cookie, once.
SESSION_KEY_PARAM = "key"


def mint_session_key() -> str:
    return secrets.token_urlsafe(32)


def link_path(port: int) -> Path:
    """Where the link that opens the review UI is left for the person to pick up."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    return base / "mailmind" / f"review-{port}.link"


def leave_the_link(port: int, url: str) -> Path:
    """Write the link somewhere only this user can read it, and return where.

    Not stderr.  An MCP client collects the stderr of everything it spawns into a log, and
    some of them put that log in front of the model — which would hand the agent the one
    thing the whole arrangement depends on it not having.  A file with a mode on it is
    readable by the person and by anything already running as them, which is the same
    boundary everything else here rests on.
    """
    path = link_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # Created empty and locked down before the key goes in, so it is never briefly
    # readable by anybody else.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(url + "\n")
    return path


async def reviewer(scope) -> m.Producer:  # noqa: ANN001
    """The person at the keyboard.

    One local reviewer in this iteration.  It is a producer row rather than a special case
    because "who accepted this" has to be answerable, and because standing authority will
    later be a different producer making the same acceptance.
    """
    person = await scope.scalar(
        sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.person)
    )
    if person is None:
        person = m.Producer(kind=m.ProducerKind.person, name="reviewer")
        scope.add(person)
        await scope.flush()
    return person


class GrantMiddleware(BaseHTTPMiddleware):
    """Resolve the bearer token, for a service with no authorization server in front of it.

    Ordinarily the SDK does this: it verifies the token, resolves it to a grant and hands
    it to the tools, and an unusable one is refused before any tool runs.  That needs an
    issuer to advertise, and there are loopback addresses the spec's list does not name —
    see :func:`mailmind.config.oauth_issuer`.  On one of those the endpoint still has to
    work, and a token from ``mailmindctl grant`` is the only way in, so it is resolved
    here instead.

    Same rule either way: nothing downstream takes a tenant or a producer as an argument,
    so an agent cannot assert either.
    """

    def __init__(self, app, service: Service) -> None:  # noqa: ANN001
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        token = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        context = await mcp_server.grant_context(self.service, token) if token else None
        reset = mcp_server.CURRENT_GRANT.set(context)
        try:
            return await call_next(request)
        finally:
            mcp_server.CURRENT_GRANT.reset(reset)


def _mount_login(app: FastAPI, service: Service, public_url: str) -> None:
    """Serve the OAuth endpoints from the root of the host, where clients look for them."""
    from mcp.server.auth.routes import (
        create_auth_routes,
        create_protected_resource_routes,
    )

    from mailmind.mcp.oauth import SCOPE, settings_and_provider

    settings, provider = settings_and_provider(service, public_url)
    routes = create_auth_routes(
        provider=provider,
        issuer_url=settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options,
    ) + create_protected_resource_routes(
        resource_url=settings.resource_server_url,
        authorization_servers=[settings.issuer_url],
        scopes_supported=[SCOPE],
        resource_name="mailmind",
    )
    app.router.routes.extend(routes)


def create_app(
    service: Service, *, with_mcp: bool = True, session_key: str | None = None
) -> FastAPI:
    """The review UI, and by default the MCP endpoint beside it.

    ``with_mcp=False`` is for ``mailmindctl mcp --serve``, where the agent is already on a
    pipe and a second way in would be surface nobody asked for.

    ``session_key`` is what the review UI is opened with.  Whoever starts the process is
    shown a link carrying it; following that link once trades it for a cookie.  Nothing
    else gets in — and in particular the key is never told to a model, which is the whole
    reason it exists.
    """
    check_exposure(service.config)
    session_key = session_key or mint_session_key()
    form_token = csrf_token(session_key)

    async def csrf_required(offered: str = Form(default="", alias="_csrf")) -> None:
        """The token every form on these pages carries, checked before the route runs."""
        if not secrets.compare_digest(offered, form_token):
            raise CsrfRefused

    changes = [Depends(csrf_required)]

    #: One lifespan for both shapes: the task runner always, the MCP session manager
    #: when the endpoint is mounted.  `mailmindctl mcp --serve` builds with
    #: `with_mcp=False` and its embedded uvicorn runs this lifespan too, so background
    #: work runs wherever a review UI does.
    def lifespan_with(mcp_session_manager):  # noqa: ANN001, ANN202
        @contextlib.asynccontextmanager
        async def lifespan(the_app):  # noqa: ANN001, ANN202
            runner = TaskRunner(service)
            async with contextlib.AsyncExitStack() as stack:
                if mcp_session_manager is not None:
                    await stack.enter_async_context(mcp_session_manager.run())
                tg = await stack.enter_async_context(asyncio.TaskGroup())
                # LIFO: the runner stops first, then the pool is handed back — a
                # pooled aiosqlite connection belongs to this loop and must not
                # outlive it.
                stack.push_async_callback(service.engine.dispose)
                await runner.start(tg)
                stack.push_async_callback(runner.stop)
                the_app.state.task_runner = runner
                yield

        return lifespan

    if not with_mcp:
        app = FastAPI(title="mailmind", lifespan=lifespan_with(None))
    else:
        public_url = oauth_issuer(service.config)
        mcp = mcp_server.build_server(service, public_url=public_url)
        # DNS rebinding protection: a browser page on some other site must not be able to
        # drive this endpoint just because it is listening on localhost.
        bind = service.config.bind
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        origins = [f"http://{host}" for host in hosts]
        if not is_wildcard(bind):
            # A wildcard is not an address anybody sends as a Host, so listing it would
            # look like protection while matching nothing.
            hosts.append(f"{bind}:*")
            origins.append(f"http://{bind}:*")
        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=hosts,
                allowed_origins=origins,
            ),
        )
        app = FastAPI(title="mailmind", lifespan=lifespan_with(mcp.session_manager))
        app.mount("/mcp", mcp_app if public_url else GrantMiddleware(mcp_app, service))

        # The SDK builds these routes too, but inside the app just mounted — so they would
        # answer at `/mcp/.well-known/...`, which is not where any client looks.  RFC 8414
        # and RFC 9728 both put them at the root of the host, so that is where they go, and
        # the copies under `/mcp` are left as unadvertised duplicates.
        #
        # None of it exists without an issuer to name, which is the loopback address the
        # spec's list does not happen to cover — see `oauth_issuer`.
        if public_url is not None:
            _mount_login(app, service, public_url)

    @app.exception_handler(CsrfRefused)
    async def _csrf_refused(request: Request, exc: CsrfRefused):  # noqa: ANN202
        await _record_refusal(service, request, "missing or wrong _csrf token")
        return HTMLResponse(
            "<h1>Not accepted</h1><p>This arrived without the token the review UI's own "
            "forms carry, so it did not come from a page this service served.</p>",
            status_code=403,
        )

    # Behind the session key on purpose: the pages that use it are, so their script is.
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    @app.middleware("http")
    async def require_session_key(request: Request, call_next):  # noqa: ANN001, ANN202
        """The review UI's login: the startup key, traded once for a session cookie.

        The key is never given to the agent — that is the whole design; see
        docs/security-model.md and docs/design/12 for what it buys and where it stops.
        """
        if is_machine_path(request.url.path):
            return await call_next(request)

        offered = request.query_params.get(SESSION_KEY_PARAM)
        if offered is not None and secrets.compare_digest(offered, session_key):
            # Trade it for a cookie and drop it out of the address, so it stops being in
            # the history, the title bar and anything that logs a URL.
            remaining = [
                (name, value)
                for name, value in request.query_params.multi_items()
                if name != SESSION_KEY_PARAM
            ]
            query = urlencode(remaining)
            response = RedirectResponse(
                request.url.path + (f"?{query}" if query else ""), status_code=303
            )
            response.set_cookie(
                SESSION_COOKIE,
                session_key,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response

        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie is None or not secrets.compare_digest(cookie, session_key):
            return HTMLResponse(
                "<h1>Not open</h1><p>The review UI is opened with the link printed where "
                "this was started — it carries a key, and following it once is the whole "
                "of the login. If it has scrolled away, <code>mailmindctl review --open"
                "</code> follows it for you.</p><p>If you are an agent: you were not "
                "given that key, on purpose. Tell the person you are working for to look "
                "at the terminal or the log where they started mailmind.</p>",
                status_code=401,
            )
        return await call_next(request)

    @app.middleware("http")
    async def require_same_origin_change(request: Request, call_next):  # noqa: ANN001, ANN202
        """Every route that changes something goes through here.

        A middleware rather than a check per route, so that a route added later is covered
        by having been added rather than by somebody remembering.  The per-form CSRF token
        is the other half, checked per route by ``csrf_required``.
        """
        if request.method == "POST" and not is_machine_path(request.url.path):
            problem = not_same_origin(request)
            if problem is not None:
                await _record_refusal(service, request, problem)
                return HTMLResponse(
                    "<h1>Not accepted</h1><p>This did not arrive from a page this service "
                    "served: " + problem + ".</p><p>The review UI is for whoever owns "
                    "this mail, at this computer. If you are an agent, this is the page "
                    "you were told to send somebody to, not one to act on — nothing here "
                    "is yours to accept.</p>",
                    status_code=403,
                )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        if service.schema_problem is not None:
            # The database moved under a live process.  Outermost, so every surface —
            # machine paths, POSTs, all of it — says so, instead of tracebacks from
            # queries the schema no longer matches.
            return unavailable_page(service.schema_problem)
        response = await call_next(request)
        if not is_machine_path(request.url.path):
            response.headers["Content-Security-Policy"] = CSP
            # `no-referrer` is the tighter-looking choice and was the wrong one: browsers
            # derive a form POST's Origin from the referrer policy, so it arrived as
            # `Origin: null` and this service refused its own buttons.  `same-origin`
            # sends a referrer to nobody but us, which is the part that matters when the
            # page is rendering links written by strangers.
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    def render(request: Request, template: str, **context) -> HTMLResponse:  # noqa: ANN003
        context.setdefault("csrf", form_token)
        return TEMPLATES.TemplateResponse(request, template, context)

    async def chrome(scope) -> dict:  # noqa: ANN001
        """What the header shows on every page: the accounts, and which one is chosen."""
        current = await chosen_account(scope)
        return {
            "accounts": await views.accounts(scope),
            "current_account": {"id": current.id, "name": current.name} if current else None,
            "waiting": await views.proposed_counts(scope),
        }

    def task_fragment(task: dict, *, streaming: bool) -> str:
        """One task's row, as Turbo will swap it: same template for route and stream."""
        return TEMPLATES.env.get_template("_task.html").render(
            task=task, csrf=form_token, streaming=streaming
        )

    @app.get("/task/{task_id}/fragment", response_class=HTMLResponse)
    async def task_fragment_page(task_id: int):  # noqa: ANN202
        async with service.scope(readonly=True) as scope:
            task = await scope.get(m.Task, task_id)
            if task is None:
                return HTMLResponse("no such task", status_code=404)
            return HTMLResponse(task_fragment(views.task_view(task), streaming=False))

    @app.post("/task/{task_id}/retry", dependencies=changes)
    async def retry_task(task_id: int):  # noqa: ANN202
        """Ask again.  Only a failed task; coalescing makes double-clicks harmless."""
        target = "/"
        async with service.scope() as scope:
            task = await scope.get(m.Task, task_id)
            if task is not None and task.status is m.TaskStatus.failed:
                if task.kind is m.TaskKind.apply_bundle:
                    bundle = await scope.get(m.Bundle, task.subject_id)
                    target = f"/bundle/{task.subject_id}"
                    if bundle is None or bundle.status is not m.BundleStatus.accepted:
                        return RedirectResponse(target, status_code=303)
                elif task.kind is m.TaskKind.fetch_body:
                    target = f"/bundle/{task.payload.get('bundle_id', '')}" or "/"
                elif task.kind in (m.TaskKind.sync_account, m.TaskKind.sync_container):
                    target = "/accounts"
                await tasks.enqueue(
                    scope,
                    kind=task.kind,
                    account_id=task.account_id,
                    subject_id=task.subject_id,
                    payload=dict(task.payload),
                    requested_by=(await reviewer(scope)).id,
                )
                await scope.commit()
        service.notify_tasks()
        return RedirectResponse(target, status_code=303)

    @app.get("/events")
    async def events(request: Request):  # noqa: ANN202
        """Progress, pushed: each event is a <turbo-stream> replacing one task's row.

        A terminal transition also sends a page refresh, so the surroundings — the
        attempts table, the queue's sections — catch up without anybody polling.
        """
        from fastapi.responses import StreamingResponse

        runner = getattr(request.app.state, "task_runner", None)

        def as_event(view: dict) -> str:
            body = task_fragment(view, streaming=True).replace("\n", " ")
            return (
                'data: <turbo-stream action="replace" '
                f'target="task-{view["id"]}"><template>{body}</template>'
                "</turbo-stream>\n\n"
            )

        async def stream():  # noqa: ANN202
            if runner is None:
                return
            queue = runner.hub.subscribe()
            try:
                # Catch up first: a page that opens the stream mid-run gets the current
                # state now rather than at the next change.
                async with service.scope(readonly=True) as scope:
                    live_now = await views.task_summaries(scope, None, live_only=True)
                for view in live_now:
                    yield as_event(view)
                while True:
                    task_id = await queue.get()
                    async with service.scope(readonly=True) as scope:
                        task = await scope.get(m.Task, task_id)
                        if task is None:
                            continue
                        view = views.task_view(task)
                    live = view["status"] in ("queued", "running")
                    yield as_event(view)
                    if not live:
                        yield 'data: <turbo-stream action="refresh"></turbo-stream>\n\n'
            finally:
                runner.hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # -------------------------------------------------------------- the queue

    @app.get("/", response_class=HTMLResponse)
    async def queue(request: Request):  # noqa: ANN202
        async with service.scope(readonly=True) as scope:
            await suggest.expire_due(scope)
            header = await chrome(scope)
            current = header["current_account"]
            # None means every account, which is only reachable when there are none at all.
            here = {current["id"]} if current else None
            # A bundle whose messages all moved on is not work anybody can do, so it stops
            # being offered here rather than after somebody opens it to find out.
            await staleness.sweep_queue(scope, here)
            await scope.commit()
            bundles = await views.bundle_summaries(
                scope, [m.BundleStatus.proposed], account_ids=here
            )
            recent = (
                await views.bundle_summaries(
                    scope,
                    [
                        m.BundleStatus.applied,
                        m.BundleStatus.partially_applied,
                        m.BundleStatus.rejected,
                        m.BundleStatus.expired,
                        m.BundleStatus.withdrawn,
                        m.BundleStatus.stale,
                    ],
                    account_ids=here,
                )
            )[:10]
            applying = await views.bundle_summaries(
                scope, [m.BundleStatus.accepted], account_ids=here
            )
            waiting_ids = {b["bundle_id"] for b in applying}
            apply_tasks = {
                task["subject_id"]: task
                for task in await views.task_summaries(
                    scope, here, kinds=[m.TaskKind.apply_bundle]
                )
                if task["subject_id"] in waiting_ids
            }
            live = any(task["status"] in ("queued", "running") for task in apply_tasks.values())
            return render(
                request,
                "queue.html",
                bundles=bundles,
                recent=recent,
                applying=applying,
                apply_tasks=apply_tasks,
                live_tasks=live,
                **header,
            )

    @app.get("/bundle/{bundle_id}", response_class=HTMLResponse)
    async def bundle_page(request: Request, bundle_id: int, error: str | None = None):  # noqa: ANN202
        async with service.scope() as scope:
            bundle = await scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            if bundle.status is m.BundleStatus.proposed:
                # The first of the two staleness checks: before it is shown.
                await staleness.refresh_bundle(scope, bundle)
                await scope.commit()
            detail = await views.bundle_detail(scope, bundle_id)
            bodies = {}
            for item in detail["items"]:
                body = await scope.scalar(
                    sa.select(m.MessageBody).where(
                        m.MessageBody.message_id == item["message_id"]
                    )
                )
                if body is not None:
                    bodies[item["message_id"]] = {
                        "text": (body.text_plain or body.text_from_html or "")[:4000],
                        "links": body.links.get("links", [])[:40],
                    }
            apply_task = None
            for task in await views.task_summaries(
                scope, None, kinds=[m.TaskKind.apply_bundle]
            ):
                if task["subject_id"] == bundle_id:
                    apply_task = task
                    break
            fetch_tasks = {}
            for task in await views.task_summaries(scope, None, kinds=[m.TaskKind.fetch_body]):
                fetch_tasks.setdefault(task["subject_id"], task)
            live = (
                apply_task is not None and apply_task["status"] in ("queued", "running")
            ) or any(task["status"] in ("queued", "running") for task in fetch_tasks.values())
            return render(
                request,
                "bundle.html",
                bundle=detail,
                bodies=bodies,
                error=error,
                apply_task=apply_task,
                fetch_tasks=fetch_tasks,
                live_tasks=live,
                **await chrome(scope),
            )

    @app.post("/bundle/{bundle_id}/body/{message_id}", dependencies=changes)
    async def load_body(bundle_id: int, message_id: int):  # noqa: ANN202
        async with service.scope() as scope:
            placement = await scope.scalar(
                views.live_placements().where(m.Placement.message_id == message_id)
            )
            if placement is not None:
                container = await scope.get(m.Container, placement.container_id)
                await tasks.enqueue(
                    scope,
                    kind=m.TaskKind.fetch_body,
                    account_id=container.account_id,
                    subject_id=message_id,
                    payload={"bundle_id": bundle_id},
                    requested_by=(await reviewer(scope)).id,
                )
                await scope.commit()
        service.notify_tasks()
        return RedirectResponse(f"/bundle/{bundle_id}#m{message_id}", status_code=303)

    @app.post("/bundle/{bundle_id}/exclude/{suggestion_id}", dependencies=changes)
    async def exclude_item(bundle_id: int, suggestion_id: int):  # noqa: ANN202
        anchor = None
        async with service.scope() as scope:
            suggestion = await scope.get(m.Suggestion, suggestion_id)
            error = None
            if suggestion is not None and suggestion.bundle_id == bundle_id:
                # Land back where the reviewer was, not at the top of a long table.
                anchor = (
                    f"m{suggestion.message_id}"
                    if suggestion.message_id
                    else f"s{suggestion_id}"
                )
                try:
                    await suggest.exclude(scope, suggestion, await reviewer(scope))
                except suggest.ProposalRefused as exc:
                    error = str(exc)
                await scope.commit()
        return _back(bundle_id, error, anchor)

    @app.post("/bundle/{bundle_id}/accept", dependencies=changes)
    async def accept_bundle(  # noqa: ANN202
        bundle_id: int,
        reviewed_through: int = Form(default=0),
        acknowledge_stale: str = Form(default=""),
    ):
        async with service.scope() as scope:
            bundle = await scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            try:
                # What the page being accepted from actually showed.  Without it, accept
                # would mean "this bundle as it stands now" rather than "the bundle I read".
                await suggest.accept(
                    scope,
                    bundle,
                    await reviewer(scope),
                    reviewed_through=reviewed_through,
                    acknowledge_stale=bool(acknowledge_stale),
                )
            except suggest.ProposalRefused as exc:
                await scope.commit()
                return _back(bundle_id, str(exc))

            # The accept is recorded; the mailbox work is the runner's.  The page shows
            # the apply as it runs, and a failure is a row with a retry — not a stuck
            # invisible bundle.
            await tasks.enqueue(
                scope,
                kind=m.TaskKind.apply_bundle,
                account_id=bundle.account_id,
                subject_id=bundle.id,
                requested_by=(await reviewer(scope)).id,
            )
            await scope.commit()
        service.notify_tasks()
        return _back(bundle_id, None)

    @app.post("/bundle/{bundle_id}/reject", dependencies=changes)
    async def reject_bundle(bundle_id: int, reason: str = Form(default="")):  # noqa: ANN202
        async with service.scope() as scope:
            bundle = await scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            error = None
            try:
                await suggest.reject(scope, bundle, await reviewer(scope), reason or None)
            except suggest.ProposalRefused as exc:
                error = str(exc)
            await scope.commit()
        return _back(bundle_id, error)

    # ------------------------------------------------------------- the mailbox

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):  # noqa: ANN202
        async with service.scope(readonly=True) as scope:
            rows = []
            for account in await scope.all(sa.select(m.Account)):
                caps = await scope.all(
                    sa.select(m.AccountCapability).where(
                        m.AccountCapability.account_id == account.id
                    )
                )
                rows.append(
                    {
                        "account": account,
                        "containers": await views.containers(scope, account.id),
                        "capabilities": sorted(
                            (c.name, c.declared, c.probed_present) for c in caps
                        ),
                    }
                )
            sync_tasks = {}
            for task in await views.task_summaries(
                scope, None, kinds=[m.TaskKind.sync_account]
            ):
                sync_tasks.setdefault(task["account_id"], task)
            return render(
                request,
                "accounts.html",
                rows=rows,
                sync_tasks=sync_tasks,
                live_tasks=any(
                    task["status"] in ("queued", "running") for task in sync_tasks.values()
                ),
                **await chrome(scope),
            )

    @app.post("/accounts/choose", dependencies=changes)
    async def choose_account(account_id: int = Form()):  # noqa: ANN202
        """Work in a different account.

        The choice belongs to the person rather than to the tenant, which is the shape it
        needs on a deployment where several authenticated people share one.
        """
        async with service.scope() as scope:
            account = await scope.get(m.Account, account_id)
            if account is not None:
                (await reviewer(scope)).current_account_id = account.id
                await scope.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/sync", dependencies=changes)
    async def sync_account(account_id: int):  # noqa: ANN202
        async with service.scope() as scope:
            account = await scope.get(m.Account, account_id)
            if account is not None:
                await tasks.enqueue(
                    scope,
                    kind=m.TaskKind.sync_account,
                    account_id=account.id,
                    subject_id=account.id,
                    requested_by=(await reviewer(scope)).id,
                )
                await scope.commit()
        service.notify_tasks()
        return RedirectResponse("/accounts", status_code=303)

    # ------------------------------------------------------------- letting an agent in

    #: What each capability means, said for somebody deciding rather than somebody
    #: implementing.  There is no entry for applying because there is no such capability.
    CAPABILITY_MEANS = {
        m.Capability.observe: "read the mail you tick below",
        m.Capability.suggest: "propose changes, for you to accept or reject here",
        m.Capability.assess: "record what it makes of a message",
    }

    async def _pending(scope, request_id: str):  # noqa: ANN001, ANN202
        """The consent request, if it is still one."""
        row = await scope.scalar(
            sa.select(m.OAuthAuthorization).where(m.OAuthAuthorization.request_id == request_id)
        )
        if row is None or row.grant_id is not None:
            return None
        if row.expires_at is not None and row.expires_at <= dt.datetime.now(dt.UTC):
            return None
        return row

    @app.get("/consent", response_class=HTMLResponse)
    async def consent_page(request: Request, req: str = "", error: str | None = None):  # noqa: ANN202
        """Where a person agrees to let an agent in.

        Reached because an agent sent a browser here, and guarded by the same key as every
        other page: an agent that followed its own link arrives without the cookie and is
        told to fetch a person, which is the correct outcome.
        """
        request_id = request.query_params.get("request", req)
        async with service.scope() as scope:
            row = await _pending(scope, request_id)
            if row is None:
                return HTMLResponse(
                    "<h1>Nothing to agree to</h1><p>That request has been answered "
                    "already, or it sat here long enough to go stale. Ask the agent to "
                    "connect again.</p>",
                    status_code=404,
                )
            client = await scope.scalar(
                sa.select(m.OAuthClient).where(m.OAuthClient.client_id == row.client_id)
            )
            return render(
                request,
                "consent.html",
                request_row={
                    "request_id": row.request_id,
                    "client_name": client.client_name if client else None,
                },
                capabilities=[
                    {"name": cap.value, "what": what} for cap, what in CAPABILITY_MEANS.items()
                ],
                # The accounts to tick are the ones in the header chrome — the same list,
                # so it is passed once rather than twice under two names.
                error=error,
                **await chrome(scope),
            )

    @app.post("/consent", dependencies=changes)
    async def decide_consent(  # noqa: ANN202
        request_id: str = Form(),
        decision: str = Form(default="refuse"),
        capabilities: list[str] = TICKED_CAPABILITIES,
        account_ids: list[int] = TICKED_ACCOUNTS,
    ):
        """Agree, or do not.

        This is the only place a grant is created from a request somebody made.  Refusing
        hands the client an error rather than leaving it waiting, because a client that is
        never answered looks to its user like a service that is broken.
        """
        async with service.scope() as scope:
            row = await _pending(scope, request_id)
            if row is None:
                return HTMLResponse(
                    "<h1>Nothing to agree to</h1><p>That request is already answered or "
                    "has gone stale.</p>",
                    status_code=404,
                )
            target = row.redirect_uri
            state = row.state

            if decision == "allow" and not account_ids:
                any_accounts = await scope.scalar(sa.select(m.Account.id).limit(1))
                if any_accounts is not None:
                    return RedirectResponse(
                        f"/consent?request={request_id}&error="
                        + quote("You ticked no mail. Tick an account, or refuse."),
                        status_code=303,
                    )

            if decision != "allow":
                await scope.audit(
                    "grant_refused",
                    actor_kind="person",
                    subject_kind="oauth_client",
                    payload={"client_id": row.client_id},
                )
                await scope.commit()
                return RedirectResponse(
                    construct_redirect_uri(target, error="access_denied", state=state),
                    status_code=303,
                )

            client = await scope.scalar(
                sa.select(m.OAuthClient).where(m.OAuthClient.client_id == row.client_id)
            )
            name = (client.client_name if client else "") or "agent"
            producer = await scope.scalar(
                sa.select(m.Producer).where(
                    m.Producer.name == name, m.Producer.kind == m.ProducerKind.agent
                )
            )
            if producer is None:
                producer = m.Producer(kind=m.ProducerKind.agent, name=name)
                scope.add(producer)
                await scope.flush()

            code = await oauth.consent(
                scope,
                row,
                producer=producer,
                capabilities=list(capabilities),
                account_ids=list(account_ids),
            )
            await scope.commit()
        return RedirectResponse(
            construct_redirect_uri(target, code=code, state=state), status_code=303
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request):  # noqa: ANN202
        """What has been let in, and the button that takes it back."""
        async with service.scope(readonly=True) as scope:
            names = {a["id"]: a["name"] for a in await views.accounts(scope)}
            clients = {
                c.client_id: c.client_name for c in await scope.all(sa.select(m.OAuthClient))
            }
            rows = []
            for grant in await scope.all(
                sa.select(m.Grant).order_by(m.Grant.created_at.desc())
            ):
                producer = await scope.get(m.Producer, grant.producer_id)
                live = mcp_server._live(grant)
                why_not = "revoked" if grant.revoked_at is not None else "expired"
                rows.append(
                    {
                        "id": grant.id,
                        "producer": producer.name if producer else "?",
                        "client_name": clients.get(grant.client_id or ""),
                        "capabilities": list(grant.capabilities),
                        "accounts": [
                            names.get(ga.account_id, str(ga.account_id))
                            for ga in grant.accounts
                        ],
                        "created_at": grant.created_at.isoformat(),
                        "how": "agreed here" if grant.client_id else "command line",
                        "live": live,
                        "why_not": why_not,
                    }
                )
            return render(request, "agents.html", grants=rows, **await chrome(scope))

    @app.post("/agents/{grant_id}/revoke", dependencies=changes)
    async def revoke_grant(grant_id: int):  # noqa: ANN202
        """Take it back.

        The grant is what is revoked, not a token: every token points at it, so there is
        nothing to hunt down and nothing that outlives the decision.
        """
        async with service.scope() as scope:
            grant = await scope.get(m.Grant, grant_id)
            if grant is not None and grant.revoked_at is None:
                grant.revoked_at = dt.datetime.now(dt.UTC)
                await scope.audit(
                    "grant_revoked",
                    actor_kind="person",
                    subject_kind="grant",
                    subject_id=grant.id,
                    payload={"client_id": grant.client_id},
                )
                await scope.commit()
        return RedirectResponse("/agents", status_code=303)

    return app


async def _record_refusal(service: Service, request: Request, problem: str) -> None:
    """Leave a mark, because a refusal is the interesting half.

    Nothing was applied, so there is no state change to explain — but something tried to
    change a mailbox without being a person, and whoever owns the mail should be able to
    find out that it happened.
    """
    with contextlib.suppress(Exception):
        async with service.scope() as scope:
            await scope.audit(
                "ui_change_refused",
                actor_kind="service",
                subject_kind="request",
                payload={
                    "path": request.url.path,
                    "problem": problem,
                    "user_agent": request.headers.get("user-agent"),
                },
            )
            await scope.commit()


def _back(bundle_id: int, error: str | None, anchor: str | None = None) -> RedirectResponse:
    target = f"/bundle/{bundle_id}"
    if error:
        target += f"?error={quote(error)}"
    if anchor:
        target += f"#{anchor}"
    return RedirectResponse(target, status_code=303)
