"""One process, two surfaces.

``/mcp`` is where agents connect, authenticated by a bearer token that resolves to a
grant.  Everything else is the review UI, which is where acceptance lives.  They share a
database and nothing else: no route under ``/mcp`` can reach the applier, and the review
routes never consult a grant.
"""

from __future__ import annotations

import contextlib
import secrets
from pathlib import Path
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from mailmind import views
from mailmind.config import check_exposure, is_wildcard
from mailmind.db import models as m
from mailmind.imap import apply as applier
from mailmind.imap import sync
from mailmind.imap.backend import TRASH
from mailmind.mcp import server as mcp_server
from mailmind.service import Service
from mailmind.suggest import model as suggest

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: No script, no remote anything.  A remote image would tell a sender their mail was read,
#: and this page renders mail written by strangers.
CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; img-src 'none'"
)


class GrantMiddleware(BaseHTTPMiddleware):
    """Resolve the bearer token before any tool runs.

    This is the only place a caller's identity is established.  Nothing downstream takes
    a tenant or a producer as an argument, so an agent cannot assert either.
    """

    def __init__(self, app, service: Service) -> None:  # noqa: ANN001
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        token = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        context = mcp_server.grant_context(self.service, token) if token else None
        reset = mcp_server.CURRENT_GRANT.set(context)
        try:
            return await call_next(request)
        finally:
            mcp_server.CURRENT_GRANT.reset(reset)


def chosen_account(scope) -> m.Account | None:  # noqa: ANN001
    """Which account the review UI is working in.

    The review UI's login is a key, not an identity: it says somebody may come in, not
    which somebody.  So there is still nobody to look up, the reviewer is still implicit,
    and what a person picks is which account they are looking at.  Authentication that
    says *who* only enters on a deployment, and when it does it replaces :func:`reviewer`
    rather than this.

    This is a view and not a boundary.  Nothing is being kept from anybody — the person
    at the keyboard owns all of it — so a link to a bundle in another account still
    works.  The account scoping that *is* a boundary is the grant's, on the agent
    surface, and it is enforced somewhere else entirely.

    Nothing chosen falls back to the first account by name, so a fresh install has one
    without anybody having to pick.
    """
    person = scope.scalar(sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.person))
    if person is not None and person.current_account_id is not None:
        chosen = scope.get(m.Account, person.current_account_id)
        if chosen is not None:
            return chosen
    return scope.scalar(sa.select(m.Account).order_by(m.Account.name))


#: What a browser sends when a person submits a form on a page it is showing. All three
#: are Fetch Metadata headers, which scripts are forbidden to set — inside a browser these
#: cannot be forged, and outside one they have to be asserted deliberately.
#:
#: `Sec-Fetch-User: ?1` would be the better signal, being "a person did this" rather than
#: "a document navigated". It is not required because Safari has never sent it, and a
#: check that locks out a whole browser is a check somebody turns off.
BROWSER_GESTURE = {
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "sec-fetch-site": "same-origin",
}


def not_a_browser_gesture(request: Request) -> str | None:
    """Why this POST does not look like a person submitting a form, if it does not.

    This is not a security boundary and cannot be one — anything that can set a header can
    say all of this. What it does is move the review UI out of reach of an agent doing the
    obvious thing with an address it was given, so that reaching it at all means asserting,
    in four headers, that a browser is showing a page to somebody. See
    docs/12-an-agent-of-your-own.md for how far that goes and what it does not cover.
    """
    for header, expected in BROWSER_GESTURE.items():
        actual = request.headers.get(header)
        if actual != expected:
            return f"{header} was {actual!r}, not {expected!r}"
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    expected_origin = f"{request.url.scheme}://{host}"
    if origin != expected_origin:
        return f"origin was {origin!r}, not {expected_origin!r}"
    return None


#: The cookie the review UI is reached with. Session-scoped, so it goes when the browser
#: does, and HttpOnly, so a page cannot read it back out — not that any page here runs
#: script, but a review UI renders mail written by strangers and the habit is cheap.
SESSION_COOKIE = "mailmind_session"

#: The query parameter that trades a key for that cookie, once.
SESSION_KEY_PARAM = "key"


def mint_session_key() -> str:
    return secrets.token_urlsafe(32)


def reviewer(scope) -> m.Producer:  # noqa: ANN001
    """The person at the keyboard.

    One local reviewer in this iteration.  It is a producer row rather than a special case
    because "who accepted this" has to be answerable, and because standing authority will
    later be a different producer making the same acceptance.
    """
    person = scope.scalar(sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.person))
    if person is None:
        person = m.Producer(kind=m.ProducerKind.person, name="reviewer")
        scope.add(person)
        scope.flush()
    return person


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

    if not with_mcp:
        app = FastAPI(title="mailmind")
    else:
        mcp = mcp_server.build_server(service)
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
        app = FastAPI(title="mailmind", lifespan=lambda _app: mcp.session_manager.run())
        app.mount("/mcp", GrantMiddleware(mcp_app, service))

    @app.middleware("http")
    async def only_somebody_holding_the_key_gets_in(request: Request, call_next):  # noqa: ANN001, ANN202
        """The review UI has a login, and this is it.

        Not a password — there is nobody to have an account, and a passphrase for a service
        on your own machine is friction protecting the wrong thing.  What there is instead
        is a key minted when the process starts and shown to whoever started it.  The point
        is not that it is hard to guess; it is that it is never given to the agent.  The
        model is told the address so it can send a person there, and told nothing that lets
        it go itself.

        This is the only check here that an agent with a network tool cannot simply talk
        its way past.  It stops being enough the moment the agent can read the terminal
        that printed the key or the files of the person who ran it — at which point it
        could resolve the mailbox password and skip mailmind altogether.  That boundary is
        how the agent is run, and it is not one this process can draw.
        """
        if request.url.path.startswith("/mcp"):
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
                samesite="lax",
                path="/",
            )
            return response

        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie is None or not secrets.compare_digest(cookie, session_key):
            return HTMLResponse(
                "<h1>Not open</h1><p>The review UI is opened with the link printed where "
                "this was started — it carries a key, and following it once is the whole "
                "of the login.</p><p>If you are an agent: you were not given that key, on "
                "purpose. Tell the person you are working for to look at the terminal or "
                "the log where they started mailmind.</p>",
                status_code=401,
            )
        return await call_next(request)

    @app.middleware("http")
    async def only_a_person_at_a_browser_changes_anything(request: Request, call_next):  # noqa: ANN001, ANN202
        """Every route that changes something goes through here.

        A middleware rather than a check per route, so that a route added later is covered
        by having been added rather than by somebody remembering.
        """
        if request.method == "POST" and not request.url.path.startswith("/mcp"):
            problem = not_a_browser_gesture(request)
            if problem is not None:
                await run_in_threadpool(_record_refusal, service, request, problem)
                return HTMLResponse(
                    "<h1>Not accepted</h1><p>This did not arrive as a person submitting a "
                    "form in a browser: " + problem + ".</p><p>The review UI is for "
                    "whoever owns this mail, at this computer. If you are an agent, this "
                    "is the page you were told to send somebody to, not one to act on — "
                    "nothing here is yours to accept.</p>",
                    status_code=403,
                )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        if not request.url.path.startswith("/mcp"):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def render(request: Request, template: str, **context) -> HTMLResponse:  # noqa: ANN003
        return TEMPLATES.TemplateResponse(request, template, context)

    def chrome(scope) -> dict:  # noqa: ANN001
        """What the header shows on every page: the accounts, and which one is chosen."""
        current = chosen_account(scope)
        return {
            "accounts": views.accounts(scope),
            "current_account": {"id": current.id, "name": current.name} if current else None,
        }

    # -------------------------------------------------------------- the queue

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request):  # noqa: ANN202
        with service.scope() as scope:
            suggest.expire_due(scope)
            scope.commit()
            header = chrome(scope)
            current = header["current_account"]
            # None means every account, which is only reachable when there are none at all.
            here = {current["id"]} if current else None
            bundles = views.bundle_summaries(scope, [m.BundleStatus.proposed], account_ids=here)
            recent = views.bundle_summaries(
                scope,
                [
                    m.BundleStatus.applied,
                    m.BundleStatus.partially_applied,
                    m.BundleStatus.rejected,
                    m.BundleStatus.expired,
                    m.BundleStatus.withdrawn,
                ],
                account_ids=here,
            )[:10]
            return render(request, "queue.html", bundles=bundles, recent=recent, **header)

    @app.get("/bundle/{bundle_id}", response_class=HTMLResponse)
    def bundle_page(request: Request, bundle_id: int, error: str | None = None):  # noqa: ANN202
        with service.scope() as scope:
            bundle = scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            if bundle.status is m.BundleStatus.proposed:
                # The first of the two staleness checks: before it is shown.
                from mailmind.suggest import staleness

                staleness.refresh_bundle(scope, bundle)
                scope.commit()
            detail = views.bundle_detail(scope, bundle_id)
            bodies = {}
            for item in detail["items"]:
                body = scope.scalar(
                    sa.select(m.MessageBody).where(
                        m.MessageBody.message_id == item["message_id"]
                    )
                )
                if body is not None:
                    bodies[item["message_id"]] = {
                        "text": (body.text_plain or body.text_from_html or "")[:4000],
                        "links": body.links.get("links", [])[:40],
                    }
            return render(
                request,
                "bundle.html",
                bundle=detail,
                bodies=bodies,
                error=error,
                **chrome(scope),
            )

    @app.post("/bundle/{bundle_id}/body/{message_id}")
    def load_body(bundle_id: int, message_id: int):  # noqa: ANN202
        with service.scope() as scope:
            placement = scope.scalar(
                views.live_placements().where(m.Placement.message_id == message_id)
            )
            if placement is not None:
                container = scope.get(m.Container, placement.container_id)
                account = scope.get(m.Account, container.account_id)
                with contextlib.suppress(Exception), service.backend(account) as backend:
                    sync.fetch_and_cache_body(
                        scope,
                        account,
                        container,
                        placement,
                        backend,
                        budget_bytes=service.config.limits.body_cache_bytes,
                    )
                scope.commit()
        return RedirectResponse(f"/bundle/{bundle_id}#m{message_id}", status_code=303)

    @app.post("/bundle/{bundle_id}/exclude/{suggestion_id}")
    def exclude_item(bundle_id: int, suggestion_id: int):  # noqa: ANN202
        with service.scope() as scope:
            suggestion = scope.get(m.Suggestion, suggestion_id)
            error = None
            if suggestion is not None and suggestion.bundle_id == bundle_id:
                try:
                    suggest.exclude(scope, suggestion, reviewer(scope))
                except suggest.ProposalRefused as exc:
                    error = str(exc)
                scope.commit()
        return _back(bundle_id, error)

    @app.post("/bundle/{bundle_id}/accept")
    def accept_bundle(bundle_id: int, acknowledge_stale: str = Form(default="")):  # noqa: ANN202
        with service.scope() as scope:
            bundle = scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            try:
                suggest.accept(
                    scope,
                    bundle,
                    reviewer(scope),
                    acknowledge_stale=bool(acknowledge_stale),
                )
            except suggest.ProposalRefused as exc:
                scope.commit()
                return _back(bundle_id, str(exc))

            account = scope.get(m.Account, bundle.account_id)
            trash = scope.scalar(
                sa.select(m.Container).where(
                    m.Container.account_id == account.id,
                    m.Container.special_use == TRASH,
                )
            )
            try:
                with service.backend(account) as backend:
                    applier.apply_bundle(scope, bundle, backend, trash_container=trash)
            except applier.NotApplicable as exc:
                scope.commit()
                return _back(bundle_id, str(exc))
            scope.commit()
        return _back(bundle_id, None)

    @app.post("/bundle/{bundle_id}/reject")
    def reject_bundle(bundle_id: int, reason: str = Form(default="")):  # noqa: ANN202
        with service.scope() as scope:
            bundle = scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            error = None
            try:
                suggest.reject(scope, bundle, reviewer(scope), reason or None)
            except suggest.ProposalRefused as exc:
                error = str(exc)
            scope.commit()
        return _back(bundle_id, error)

    # ------------------------------------------------------------- the mailbox

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):  # noqa: ANN202
        with service.scope() as scope:
            rows = []
            for account in scope.scalars(sa.select(m.Account)):
                caps = scope.scalars(
                    sa.select(m.AccountCapability).where(
                        m.AccountCapability.account_id == account.id
                    )
                ).all()
                rows.append(
                    {
                        "account": account,
                        "containers": views.containers(scope, account.id),
                        "capabilities": sorted(
                            (c.name, c.declared, c.probed_present) for c in caps
                        ),
                    }
                )
            return render(request, "accounts.html", rows=rows, **chrome(scope))

    @app.post("/accounts/choose")
    def choose_account(account_id: int = Form()):  # noqa: ANN202
        """Work in a different account.

        The choice belongs to the person rather than to the tenant, which is the shape it
        needs on a deployment where several authenticated people share one.
        """
        with service.scope() as scope:
            account = scope.get(m.Account, account_id)
            if account is not None:
                reviewer(scope).current_account_id = account.id
                scope.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/sync")
    def sync_account(account_id: int):  # noqa: ANN202
        with service.scope() as scope:
            account = scope.get(m.Account, account_id)
            if account is not None:
                with service.backend(account) as backend:
                    for container in sync.discover_containers(scope, account, backend):
                        if container.selectable:
                            sync.sync_container(scope, account, container, backend)
                            # Per folder: see the same commit in `mailmindctl sync`.
                            scope.commit()
                scope.commit()
        return RedirectResponse("/accounts", status_code=303)

    return app


def _record_refusal(service: Service, request: Request, problem: str) -> None:
    """Leave a mark, because a refusal is the interesting half.

    Nothing was applied, so there is no state change to explain — but something tried to
    change a mailbox without being a person, and whoever owns the mail should be able to
    find out that it happened.
    """
    with contextlib.suppress(Exception), service.scope() as scope:
        scope.audit(
            "ui_change_refused",
            actor_kind="service",
            subject_kind="request",
            payload={
                "path": request.url.path,
                "problem": problem,
                "user_agent": request.headers.get("user-agent"),
            },
        )
        scope.commit()


def _back(bundle_id: int, error: str | None) -> RedirectResponse:
    from urllib.parse import quote

    target = f"/bundle/{bundle_id}"
    if error:
        target += f"?error={quote(error)}"
    return RedirectResponse(target, status_code=303)
