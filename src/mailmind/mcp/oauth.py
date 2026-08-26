"""How an agent comes by a grant, when nobody is copying a token out of a terminal.

This is the authorization server.  It issues tokens for :mod:`mailmind.mcp.server`, and it
is deliberately *here* rather than delegated to whatever identity provider is nearby: an
identity provider answers "who is this person", and what ``/mcp`` needs answered is "may
this agent hold this grant", which is about mailmind's grant model and nothing an identity
provider knows.  See ``docs/design/13-logging-an-agent-in.md``.

Nothing in this module decides what an agent may do.  It decides which grant a request
carries; the grant decides the rest, and :func:`mailmind.mcp.server._require` is still the
only thing that checks a capability.  The single scope below exists because the protocol
wants one, and it carries no information at all.

The person is never here either.  Consent happens on a page in the review UI — see
:mod:`mailmind.web.app` — because agreeing is a thing a person does, and this module has
no way to ask anybody anything.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import sqlalchemy as sa
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from mailmind.db import models as m
from mailmind.service import Service, hash_token, mint_token

#: The one scope, carried by every token.  The grant says what the token may do, and the
#: person chose that at the consent page (docs/design/05: the view is given, not chosen).
SCOPE = "mailmind"

#: An access token is short because it can be: the client registered for `refresh_token`
#: precisely so that rotating one is invisible.
ACCESS_TTL = dt.timedelta(hours=1)
REFRESH_TTL = dt.timedelta(days=30)
#: How long a consent page stays answerable.  Somebody who wandered off mid-flow comes back
#: to a request that has gone stale rather than one that still grants a mailbox.
REQUEST_TTL = dt.timedelta(minutes=10)
#: Seconds between clicking allow and the client redeeming the code.  It is a redirect, so
#: this is generous.
CODE_TTL = dt.timedelta(minutes=5)


class GrantAccessToken(AccessToken):
    """An access token that already knows which grant it resolved to.

    The SDK permits fields on a subclass — it never renders these types into a response —
    and carrying the grant here means it is looked up once, when the token is verified,
    rather than again on every tool call.
    """

    grant_view: dict[str, Any] | None = None


def _epoch(when: dt.datetime | None) -> float | None:
    return None if when is None else when.timestamp()


def _live_token(row: m.OAuthToken | None) -> bool:
    if row is None or row.revoked_at is not None:
        return False
    return row.expires_at is None or row.expires_at > dt.datetime.now(dt.UTC)


class MailmindAuthorizationServer:
    """Implements the SDK's ``OAuthAuthorizationServerProvider`` against the mailmind tables.

    Every method here runs the database work synchronously.  That matches what the rest of
    the service does under Starlette, and these are single-row lookups against SQLite.
    """

    def __init__(self, service: Service) -> None:
        self.service = service

    # ------------------------------------------------------------------ clients

    def _client_info(self, row: m.OAuthClient) -> OAuthClientInformationFull:
        return OAuthClientInformationFull.model_validate(
            {
                "client_id": row.client_id,
                "client_name": row.client_name or None,
                "redirect_uris": list(row.redirect_uris),
                "grant_types": list(row.grant_types),
                "response_types": list(row.response_types),
                "scope": row.scope,
                "token_endpoint_auth_method": row.token_endpoint_auth_method,
                # Never stored, so never echoed.  A client that lost its secret registers
                # again; there is nothing here to remind it.
                "client_secret": None,
            }
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.service.scope() as s:
            row = s.scalar(sa.select(m.OAuthClient).where(m.OAuthClient.client_id == client_id))
            return self._client_info(row) if row is not None else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self.service.scope() as s:
            row = m.OAuthClient(
                client_id=client_info.client_id,
                client_secret_hash=(
                    hash_token(client_info.client_secret) if client_info.client_secret else None
                ),
                client_name=client_info.client_name or "",
                redirect_uris=[str(uri) for uri in (client_info.redirect_uris or [])],
                grant_types=list(client_info.grant_types),
                response_types=list(client_info.response_types),
                scope=client_info.scope,
                token_endpoint_auth_method=client_info.token_endpoint_auth_method or "none",
            )
            s.add(row)
            s.audit(
                "oauth_client_registered",
                actor_kind="service",
                subject_kind="oauth_client",
                payload={
                    "client_id": client_info.client_id,
                    # Recorded as what it is: a name the client chose for itself.
                    "client_name_claimed": client_info.client_name,
                },
            )
            s.commit()

    # ------------------------------------------------------------ authorization

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the consent page.

        No code is issued here and no grant exists yet.  Both are made when somebody
        agrees, because until then there is nothing to point at.
        """
        request_id = secrets.token_urlsafe(24)
        with self.service.scope() as s:
            s.add(
                m.OAuthAuthorization(
                    request_id=request_id,
                    client_id=client.client_id,
                    redirect_uri=str(params.redirect_uri),
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    state=params.state,
                    code_challenge=params.code_challenge,
                    scopes=list(params.scopes or [SCOPE]),
                    resource=params.resource,
                    expires_at=dt.datetime.now(dt.UTC) + REQUEST_TTL,
                )
            )
            s.commit()
        return f"/consent?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self.service.scope() as s:
            row = s.scalar(
                sa.select(m.OAuthAuthorization).where(
                    m.OAuthAuthorization.code_hash == hash_token(authorization_code)
                )
            )
            if row is None or row.client_id != client.client_id:
                return None
            # Redeemable once.  A second attempt is somebody replaying, not traffic.
            if row.used_at is not None or row.grant_id is None:
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=list(row.scopes),
                expires_at=_epoch(row.code_expires_at) or 0.0,
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
            )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Spend the code.

        The SDK has already checked the expiry, the redirect URI and the PKCE verifier by
        the time this runs; what is left is to make sure it is spent exactly once.
        """
        with self.service.scope() as s:
            row = s.scalar(
                sa.select(m.OAuthAuthorization).where(
                    m.OAuthAuthorization.code_hash == hash_token(authorization_code.code)
                )
            )
            if row is None or row.used_at is not None or row.grant_id is None:
                raise TokenError("invalid_grant", "that code has been used or never existed")
            row.used_at = dt.datetime.now(dt.UTC)
            token = self._issue(s, grant_id=row.grant_id, client_id=row.client_id)
            s.commit()
            return token

    # ------------------------------------------------------------------ tokens

    def _issue(self, s: Any, *, grant_id: int, client_id: str) -> OAuthToken:
        """One access token and one refresh token, both pointing at the same grant."""
        now = dt.datetime.now(dt.UTC)
        access, refresh = mint_token(), mint_token()
        s.add(
            m.OAuthToken(
                token_hash=hash_token(access),
                kind=m.OAuthTokenKind.access,
                grant_id=grant_id,
                client_id=client_id,
                expires_at=now + ACCESS_TTL,
            )
        )
        s.add(
            m.OAuthToken(
                token_hash=hash_token(refresh),
                kind=m.OAuthTokenKind.refresh,
                grant_id=grant_id,
                client_id=client_id,
                expires_at=now + REFRESH_TTL,
            )
        )
        return OAuthToken(
            access_token=access,
            expires_in=int(ACCESS_TTL.total_seconds()),
            scope=SCOPE,
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with self.service.scope() as s:
            row = s.scalar(
                sa.select(m.OAuthToken).where(
                    m.OAuthToken.token_hash == hash_token(refresh_token),
                    m.OAuthToken.kind == m.OAuthTokenKind.refresh,
                )
            )
            if not _live_token(row) or row.client_id != client.client_id:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=[SCOPE],
                expires_at=int(row.expires_at.timestamp()) if row.expires_at else None,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate.

        The presented refresh token is revoked and a new pair issued against the same
        grant.  Rotating means a stolen refresh token stops working the moment the real
        client uses its own, rather than quietly working alongside it.  The grant is not
        touched: what a person consented to is not re-decided every hour.
        """
        with self.service.scope() as s:
            row = s.scalar(
                sa.select(m.OAuthToken).where(
                    m.OAuthToken.token_hash == hash_token(refresh_token.token),
                    m.OAuthToken.kind == m.OAuthTokenKind.refresh,
                )
            )
            if not _live_token(row):
                raise TokenError("invalid_grant", "that refresh token is spent")
            grant = s.get(m.Grant, row.grant_id)
            if grant is None or grant.revoked_at is not None:
                raise TokenError("invalid_grant", "the grant behind that token is gone")
            row.revoked_at = dt.datetime.now(dt.UTC)
            token = self._issue(s, grant_id=row.grant_id, client_id=row.client_id)
            s.commit()
            return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Resolve a bearer token to a grant, whichever way it was come by.

        Two shapes reach here.  An OAuth access token names a row in ``oauth_token``, which
        names a grant.  A token from ``mailmindctl grant`` *is* the grant, and has to keep
        working — turning authentication on would otherwise break every client configured
        the old way, silently, since the SDK rejects anything this method does not return.
        """
        from mailmind.mcp import server as mcp_server

        hashed = hash_token(token)
        with self.service.scope() as s:
            row = s.scalar(
                sa.select(m.OAuthToken).where(
                    m.OAuthToken.token_hash == hashed,
                    m.OAuthToken.kind == m.OAuthTokenKind.access,
                )
            )
            if _live_token(row):
                grant = s.get(m.Grant, row.grant_id)
                if mcp_server._live(grant):
                    return GrantAccessToken(
                        token=token,
                        client_id=row.client_id,
                        scopes=[SCOPE],
                        expires_at=int(row.expires_at.timestamp()) if row.expires_at else None,
                        grant_view=mcp_server._view(grant),
                    )
                return None

            # The other shape: a grant minted on the command line and handed over by hand.
            grant = s.scalar(sa.select(m.Grant).where(m.Grant.token_hash == hashed))
            if mcp_server._live(grant):
                return GrantAccessToken(
                    token=token,
                    client_id=grant.client_id or "cli",
                    scopes=[SCOPE],
                    expires_at=int(grant.expires_at.timestamp()) if grant.expires_at else None,
                    grant_view=mcp_server._view(grant),
                )
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke one credential.

        Only the credential.  Taking back what was agreed to is revoking the grant, which
        is a thing a person does on the agents page, not something a client asks for.
        """
        with self.service.scope() as s:
            row = s.scalar(
                sa.select(m.OAuthToken).where(
                    m.OAuthToken.token_hash == hash_token(token.token)
                )
            )
            if row is not None and row.revoked_at is None:
                row.revoked_at = dt.datetime.now(dt.UTC)
                s.commit()


def settings_and_provider(service: Service, public_url: str) -> tuple[Any, Any]:
    """The OAuth configuration, in the one place both users of it read from.

    The MCP server needs it to know what to reject; the web app needs it to serve the
    metadata that says where to log in.  They must agree exactly — a client discovers
    endpoints from one and is refused by the other — so they are built here rather than
    written down twice.
    """
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )

    base = public_url.rstrip("/")
    settings = AuthSettings(
        issuer_url=base,
        resource_server_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
        # Empty on purpose.  A required scope would be a second place deciding what a
        # caller may do, and the capability check on every tool is the first.
        required_scopes=[],
    )
    return settings, MailmindAuthorizationServer(service)


def consent(
    s: Any,
    request_row: m.OAuthAuthorization,
    *,
    producer: m.Producer,
    capabilities: list[str],
    account_ids: list[int],
) -> str:
    """Agree to a request: make the grant, and issue the code that hands it over.

    Called from the review UI and nowhere else.  Returns the authorization code, which the
    caller puts in the redirect back to the client.
    """
    now = dt.datetime.now(dt.UTC)
    grant = m.Grant(
        producer_id=producer.id,
        # There is no token on this grant that anybody holds: what is handed out is an
        # `oauth_token` pointing at it.  A value is still needed because the column is
        # unique and not null, so one is generated and dropped on the floor.
        token_hash=hash_token(mint_token()),
        capabilities=list(capabilities),
        client_id=request_row.client_id,
    )
    s.add(grant)
    s.flush()
    for account_id in account_ids:
        s.add(m.GrantAccount(grant_id=grant.id, account_id=account_id))

    code = mint_token()
    request_row.grant_id = grant.id
    request_row.code_hash = hash_token(code)
    request_row.code_expires_at = now + CODE_TTL
    s.audit(
        "grant_consented",
        actor_kind="person",
        subject_kind="grant",
        subject_id=grant.id,
        payload={
            "client_id": request_row.client_id,
            "capabilities": list(capabilities),
            "accounts": len(account_ids),
        },
    )
    return code
