"""One process, two surfaces.

``/mcp`` is where agents connect, authenticated by a bearer token that resolves to a
grant.  Everything else is the review UI, which is where acceptance lives.  They share a
database and nothing else: no route under ``/mcp`` can reach the applier, and the review
routes never consult a grant.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings
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

    Local review has no login, so there is nobody to look up and nothing to derive this
    from: the reviewer is implicit, and what a person picks instead is which account they
    are looking at.  Authentication only enters on a deployment, and when it does it
    replaces :func:`reviewer` rather than this.

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


def create_app(service: Service) -> FastAPI:
    # Every deployment comes through here, so this is where the bargain is checked: the
    # review UI has no login, so it does not get to listen anywhere but this machine.
    check_exposure(service.config)

    mcp = mcp_server.build_server(service)
    # DNS rebinding protection: a browser page on some other site must not be able to
    # drive this endpoint just because it is listening on localhost.
    bind = service.config.bind
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = [f"http://{host}" for host in hosts]
    if not is_wildcard(bind):
        # A wildcard is not an address anybody sends as a Host, so listing it would look
        # like protection while matching nothing.
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


def _back(bundle_id: int, error: str | None) -> RedirectResponse:
    from urllib.parse import quote

    target = f"/bundle/{bundle_id}"
    if error:
        target += f"?error={quote(error)}"
    return RedirectResponse(target, status_code=303)
