# 13 — Logging an agent in

> `#tried` `#open-questions`. How an agent comes by a grant without anybody copying a
> token out of a terminal: mailmind as the authorization server, and consent as a page in
> the review UI.

[12](12-an-agent-of-your-own.md) has the agent present a bearer token to `/mcp/`. Where
that token comes from is `mailmindctl grant`, printed once, and then it is the operator's
problem — an environment variable, a file, a password manager. That is the part this
document replaces. Nothing about [05](05-agent-surface.md) changes: the grant is still the
whole of what an agent gets, and there is still no apply tool. What changes is the walk
from "I have started an agent" to "the agent holds a grant".

## Two logins, because there are two kinds of caller

A person and an agent are not the same sort of thing and should not authenticate the same
way. [11](11-deployment-and-identity.md) already says the person's answer is a key: *may
you come in*, never *which somebody*. That stays exactly as it is.

The agent's answer becomes OAuth. It is the protocol its clients already speak — an MCP
client discovers it from a `401`, registers itself, and comes back with a token — so the
alternative is not "something simpler", it is "every operator wires a secret by hand,
differently, once per client". The failure that prompted this is the ordinary one: a
config referring to an environment variable nothing sets, which presents as *cannot
connect* rather than as *unset*.

## mailmind is the authorization server

Even on a deployment where Authelia is the SSO. That is the opposite of what
[11](11-deployment-and-identity.md) says about identity, and it is not a contradiction,
because these are different questions. Authelia answers *who is this person*. What `/mcp`
needs answered is *may this agent hold this grant* — which is about mailmind's own grant
model and nothing an identity provider knows or should know.

If Authelia were the authorization server, mailmind would have to validate its tokens and
map claims onto capabilities, and every deployment would need that mapping configured.
With mailmind as the authorization server there is nothing to map: the token it issues
*names a grant*, because mailmind is what issues grants.

This is also the answer to an open question in
[11](11-deployment-and-identity.md) — that minting on the command line is the wrong place,
but "the review UI mints agent tokens" hands the UI a power the rest of the design is
careful about. It does not, in this shape. The UI never mints anything on its own; it
consents to a request an agent has already made, and decides itself what that request
gets. Consent is a narrower power than minting, and it is the one the review UI is for.

## Consent is a page in the review UI

This is the whole of why the two logins do not become two systems.

The SDK's `/authorize` hands off to a URL of our choosing. That URL is a mailmind page, and
so it sits behind the middleware that already guards every non-`/mcp` path. Locally that is
the session key: you followed the link, so you are in; you did not, so you get the existing
*Not open* page, which already says where the link is and already says it to agents. The
POST that grants consent goes through `not_a_browser_gesture` like every other change.

On a deployment, Authelia is in front of the review UI, so it is in front of the consent
page, and nothing in mailmind knows the difference. `behind_auth_proxy` remains the only
seam. **One implementation, both modes** — which is the reason for putting the
authorization server here rather than reaching for the identity provider that happens to
be nearby.

## What the client actually asks for

Measured against opencode 1.18.20, which is the client this is for:

- a **public client** — `token_endpoint_auth_method: "none"`, no secret, PKCE carrying the
  proof instead. The SDK issues no secret for `none` and skips the secret check, so this
  needs nothing special.
- **dynamic registration**, so `ClientRegistrationOptions(enabled=True)`. It registers as
  `client_name: "OpenCode"`, which is a claim and not a fact — the same trust as
  `--producer` today, and worth writing down rather than relying on.
- `grant_types: ["authorization_code", "refresh_token"]`. Refresh is not optional: both
  `load_refresh_token` and `exchange_refresh_token` have to work.
- a **loopback redirect**, `http://127.0.0.1:<port>/oauth/callback`, on a port it picks.
  The registered `redirect_uris` are what gets checked; the port varies per run.

## A token is not a grant

The tempting shortcut is to make the access token *be* the grant: the row already holds a
token hash, an expiry and a revocation, and `grant_context` already turns a presented token
into what the tools need. It is the wrong shape, and the reason is refresh. A client
rotates hourly, so a grant-as-token would leave a fresh row every hour and "what did I
agree to" would stop being answerable from the grant table — the one question the table
exists to answer.

So the grant is the decision, and `oauth_token` rows are the credentials pointing at it.
Rotating replaces a credential; the decision is untouched, and revoking it kills everything
under it without having to find them. `oauth_client` and `oauth_authorization` are the rest:
who registered, and one trip from `/authorize` through consent to a redeemable code.

Nine methods on `OAuthAuthorizationServerProvider`, one consent template, four routes. No
reference implementation ships with the SDK, so it is written rather than adopted.

## What moved

- `create_auth_routes` adds the metadata routes to the app the SDK builds, and that app is
  mounted at `/mcp` — so they answered at `/mcp/.well-known/…`, where no client looks. They
  are built again on the FastAPI app instead, at the root of the host where RFC 8414 and
  RFC 9728 put them. The SDK's copies are left where they are, unadvertised.
- the key middleware exempted everything under `/mcp` and guarded everything else. The
  machine endpoints — `/authorize`, `/token`, `/register`, `/revoke` and the well-knowns —
  had to join it, while `/consent` had to stay guarded. It is a list rather than a prefix
  now, so that the test asserting `/consent` is *not* on it can exist.
- `GrantMiddleware` used to resolve the bearer token. The SDK does that now, and
  `_grant()` reads what it resolved. The middleware survives for the one case with no
  authorization server to configure — see the first open question below.

Stdio is untouched. There is no token on a pipe and no use for one — see
[12](12-an-agent-of-your-own.md).

## What was decided

- **One scope**, carrying no information. The agent asks for nothing in particular and a
  person ticks capabilities on the consent page — the same rule as everywhere else here,
  that the view is given and not chosen. `required_scopes` is empty and `_require` remains
  the only thing that checks a capability.
- **A new grant per consent.** Revocation is then per-decision, and the agents page lists
  one row per thing somebody agreed to.
- **Accounts are ticked too.** `GrantAccount` already means *no rows is no mail, not all
  mail*, and a consent page that does not ask would quietly grant the whole mailbox.
- **`mailmindctl grant` stays.** It is how you drive the endpoint from `curl` and the only
  way in for a client with no OAuth. Its tokens resolve through the same path, which is
  the regression that would have been quietest.
- **A token is not a grant.** Access and refresh tokens are rows pointing at the grant, so
  refreshing rotates a credential without re-deciding anything. Access an hour, refresh
  thirty days, rotating on use.
- **The client's name is shown as a claim**, because that is what it is.

## Meets

- [05](05-agent-surface.md) — the grant this hands over, and why it is the whole list
- [11](11-deployment-and-identity.md) — the key, `behind_auth_proxy`, and the open question
  this answers
- [12](12-an-agent-of-your-own.md) — the agent that connects, and the bearer token this
  replaces

## Open questions

- An issuer may not be plain HTTP anywhere but loopback, and the spec's loopback list is
  shorter than loopback: `127.0.0.2` is not on it. A deployment that cannot name a valid
  issuer is refused at startup; an unusual loopback bind runs without OAuth. The second
  half is a quiet state, and it is not obvious it should be.
- Nothing expires an `oauth_authorization` row that was never answered, or a token whose
  grant was revoked. They are harmless and they accumulate.
- Consent creates a producer named after the client, so two agents calling themselves the
  same thing share one. That is probably right — it is the same name in the queue — but it
  has not been used in anger.
- A long sync can outlive an access token. The client refreshes transparently, so this
  should be invisible; it has not been watched happening.
- `client_name` is asserted by the client and checked by nobody. Over loopback that is the
  same trust as the pipe. On a deployment it is worth knowing it is not evidence.
- The SDK also serves the OAuth routes under `/mcp`, where nothing looks. They are left as
  unadvertised duplicates rather than stripped.
