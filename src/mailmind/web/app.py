"""One process, two surfaces.

``/mcp`` is where agents connect, authenticated by a bearer token that resolves to a
grant.  Everything else is the review UI, which is where acceptance lives.  They share a
database and nothing else: no route under ``/mcp`` can reach the applier, and the review
routes never consult a grant.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from mailmind import views
from mailmind.config import check_exposure, is_wildcard, oauth_issuer
from mailmind.db import models as m
from mailmind.imap import apply as applier
from mailmind.imap import sync
from mailmind.imap.backend import TRASH, MailboxUnhealthy
from mailmind.mcp import oauth
from mailmind.mcp import server as mcp_server
from mailmind.service import Service
from mailmind.suggest import model as suggest
from mailmind.suggest import staleness

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: No script, no remote anything.  A remote image would tell a sender their mail was read,
#: and this page renders mail written by strangers.
CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; img-src 'none'"
)


def chosen_account(scope) -> m.Account | None:  # noqa: ANN001
    """Which account the review UI is working in — a view, not a boundary
    (docs/design/11 has the argument).

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
    docs/design/12-an-agent-of-your-own.md for how far that goes and what it does not cover.
    """
    for header, expected in BROWSER_GESTURE.items():
        actual = request.headers.get(header)
        if actual != expected:
            return f"{header} was {actual!r}, not {expected!r}"
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    expected_origin = f"{request.url.scheme}://{host}"
    # A browser is allowed to withhold the origin of a same-origin form submission, and
    # does: the value is derived from the referrer policy, so a strict enough one makes
    # every button in this UI arrive with `Origin: null`.  That is not evidence of
    # anything, and refusing it refuses the person.  An origin that is present and
    # *different* is a different matter, and still refused.
    if origin not in (None, "null", expected_origin):
        return f"origin was {origin!r}, not {expected_origin!r}"
    return None


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
        context = mcp_server.grant_context(self.service, token) if token else None
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

    if not with_mcp:
        app = FastAPI(title="mailmind")
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
        app = FastAPI(title="mailmind", lifespan=lambda _app: mcp.session_manager.run())
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
        if request.method == "POST" and not is_machine_path(request.url.path):
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
            header = chrome(scope)
            current = header["current_account"]
            # None means every account, which is only reachable when there are none at all.
            here = {current["id"]} if current else None
            # A bundle whose messages all moved on is not work anybody can do, so it stops
            # being offered here rather than after somebody opens it to find out.
            staleness.sweep_queue(scope, here)
            scope.commit()
            bundles = views.bundle_summaries(scope, [m.BundleStatus.proposed], account_ids=here)
            recent = views.bundle_summaries(
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
    def accept_bundle(  # noqa: ANN202
        bundle_id: int,
        reviewed_through: int = Form(default=0),
        acknowledge_stale: str = Form(default=""),
    ):
        with service.scope() as scope:
            bundle = scope.get(m.Bundle, bundle_id)
            if bundle is None:
                return HTMLResponse("no such bundle", status_code=404)
            try:
                # What the page being accepted from actually showed.  Without it, accept
                # would mean "this bundle as it stands now" rather than "the bundle I read".
                suggest.accept(
                    scope,
                    bundle,
                    reviewer(scope),
                    reviewed_through=reviewed_through,
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
            except MailboxUnhealthy as exc:
                # The accept stands and is recorded; what failed is reaching the mailbox.
                note_unhealthy(scope, account, exc)
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
                try:
                    with service.backend(account) as backend:
                        for container in sync.discover_containers(scope, account, backend):
                            if container.selectable:
                                sync.sync_container(scope, account, container, backend)
                                # Per folder: see the same commit in `mailmindctl sync`.
                                scope.commit()
                except MailboxUnhealthy as exc:
                    # A mailbox that cannot be reached is news about the mailbox, not a
                    # failure of this request. The page it returns to shows what happened.
                    note_unhealthy(scope, account, exc)
                scope.commit()
        return RedirectResponse("/accounts", status_code=303)

    # ------------------------------------------------------------- letting an agent in

    #: What each capability means, said for somebody deciding rather than somebody
    #: implementing.  There is no entry for applying because there is no such capability.
    CAPABILITY_MEANS = {
        m.Capability.observe: "read the mail you tick below",
        m.Capability.suggest: "propose changes, for you to accept or reject here",
        m.Capability.assess: "record what it makes of a message",
    }

    def _pending(scope, request_id: str):  # noqa: ANN001, ANN202
        """The consent request, if it is still one."""
        row = scope.scalar(
            sa.select(m.OAuthAuthorization).where(m.OAuthAuthorization.request_id == request_id)
        )
        if row is None or row.grant_id is not None:
            return None
        if row.expires_at is not None and row.expires_at <= dt.datetime.now(dt.UTC):
            return None
        return row

    @app.get("/consent", response_class=HTMLResponse)
    def consent_page(request: Request, req: str = "", error: str | None = None):  # noqa: ANN202
        """Where a person agrees to let an agent in.

        Reached because an agent sent a browser here, and guarded by the same key as every
        other page: an agent that followed its own link arrives without the cookie and is
        told to fetch a person, which is the correct outcome.
        """
        request_id = request.query_params.get("request", req)
        with service.scope() as scope:
            row = _pending(scope, request_id)
            if row is None:
                return HTMLResponse(
                    "<h1>Nothing to agree to</h1><p>That request has been answered "
                    "already, or it sat here long enough to go stale. Ask the agent to "
                    "connect again.</p>",
                    status_code=404,
                )
            client = scope.scalar(
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
                **chrome(scope),
            )

    @app.post("/consent")
    def decide_consent(  # noqa: ANN202
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
        with service.scope() as scope:
            row = _pending(scope, request_id)
            if row is None:
                return HTMLResponse(
                    "<h1>Nothing to agree to</h1><p>That request is already answered or "
                    "has gone stale.</p>",
                    status_code=404,
                )
            target = row.redirect_uri
            state = row.state

            if decision != "allow":
                scope.audit(
                    "grant_refused",
                    actor_kind="person",
                    subject_kind="oauth_client",
                    payload={"client_id": row.client_id},
                )
                scope.commit()
                return RedirectResponse(
                    construct_redirect_uri(target, error="access_denied", state=state),
                    status_code=303,
                )

            client = scope.scalar(
                sa.select(m.OAuthClient).where(m.OAuthClient.client_id == row.client_id)
            )
            name = (client.client_name if client else "") or "agent"
            producer = scope.scalar(
                sa.select(m.Producer).where(
                    m.Producer.name == name, m.Producer.kind == m.ProducerKind.agent
                )
            )
            if producer is None:
                producer = m.Producer(kind=m.ProducerKind.agent, name=name)
                scope.add(producer)
                scope.flush()

            code = oauth.consent(
                scope,
                row,
                producer=producer,
                capabilities=list(capabilities),
                account_ids=list(account_ids),
            )
            scope.commit()
        return RedirectResponse(
            construct_redirect_uri(target, code=code, state=state), status_code=303
        )

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):  # noqa: ANN202
        """What has been let in, and the button that takes it back."""
        with service.scope() as scope:
            names = {a["id"]: a["name"] for a in views.accounts(scope)}
            clients = {
                c.client_id: c.client_name for c in scope.scalars(sa.select(m.OAuthClient))
            }
            rows = []
            for grant in scope.scalars(sa.select(m.Grant).order_by(m.Grant.created_at.desc())):
                producer = scope.get(m.Producer, grant.producer_id)
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
            return render(request, "agents.html", grants=rows, **chrome(scope))

    @app.post("/agents/{grant_id}/revoke")
    def revoke_grant(grant_id: int):  # noqa: ANN202
        """Take it back.

        The grant is what is revoked, not a token: every token points at it, so there is
        nothing to hunt down and nothing that outlives the decision.
        """
        with service.scope() as scope:
            grant = scope.get(m.Grant, grant_id)
            if grant is not None and grant.revoked_at is None:
                grant.revoked_at = dt.datetime.now(dt.UTC)
                scope.audit(
                    "grant_revoked",
                    actor_kind="person",
                    subject_kind="grant",
                    subject_id=grant.id,
                    payload={"client_id": grant.client_id},
                )
                scope.commit()
        return RedirectResponse("/agents", status_code=303)

    return app


def note_unhealthy(scope, account, exc: Exception) -> None:  # noqa: ANN001
    """Write down that the mailbox could not be reached, where the person will see it.

    The accounts page already renders ``health`` and ``health_detail``, so a mailbox that
    has stopped answering says so there rather than in a log nobody is reading — and an
    account that is not ``ok`` is one no suggestion is applied against, which is the
    behaviour wanted while it is broken.
    """
    account.health = m.AccountHealth.down
    account.health_detail = str(exc)
    account.health_checked_at = dt.datetime.now(dt.UTC)


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
