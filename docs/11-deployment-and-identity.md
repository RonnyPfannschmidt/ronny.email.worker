# 11 — Deployment and identity

> `#sketch` `#open-questions`. Who is at the keyboard, who authenticates them, and where a
> mailbox credential lives.

[07](07-tenancy.md) asks who a row belongs to. This asks something narrower and more
immediate: who is allowed to press accept, and how the service knows. The two questions
meet at the review UI, which today has no login at all.

## Two modes, one bargain

**Local.** One tenant, no login, and the service refuses to listen anywhere but this
machine. A login on a developer's own box is ceremony protecting nothing — the person at
the keyboard is the only person there. That is a defensible position, but only while the
other half holds: nothing else may reach it.

Only half of that was ever written down. `bind` defaulted to `127.0.0.1`, and nothing
stopped it being changed, so `--host 0.0.0.0` served an unauthenticated accept-and-apply
button to the network and said nothing about it. It now refuses:

```
$ mailmindctl serve --host 0.0.0.0
Error: refusing to listen on 0.0.0.0: the review UI has no login, so anyone who
can reach it can accept a suggestion and change somebody's mail. …
```

Loopback means provably loopback — `127.0.0.0/8`, `::1`, `localhost`. A hostname is not
resolved to find out, because it could resolve to anything later and the check is meant to
be sure rather than accommodating.

**Shared.** Anything else authenticates through somebody else. mailmind does not grow a
user table, a password reset, or a session cookie of its own; there is nothing here that
does identity better than the things that exist to do identity, and a review UI that
invented its own would be the weakest part of a design whose whole argument is about who
is allowed to change what.

Concretely that is forward auth: a reverse proxy — Authelia, oauth2-proxy, an
identity-aware proxy — authenticates every request before it arrives. `behind_auth_proxy =
true` is how an operator says so. It is an **assertion, not a feature**: nothing in this
process can verify that anything is in front of it, which is exactly why it has to be
written down rather than inferred from the bind address.

## Where a mail credential lives

A mailbox password is a credential mailmind holds *on the user's behalf* and replays to a
third party. That is a different thing from the credential that authenticates the user
*to* mailmind, and it is worth being clear that an identity provider is not a place to put
it.

**Keycloak's vault is not for users.** It resolves `${vault.…}` in exactly three
administrator-supplied fields — the SMTP password, the LDAP bind credential, and an OIDC
identity provider's client secret — from a file-based or Java-KeyStore-based store the
operator populates. There is no per-user dimension to it. Keycloak *user attributes* can
hold arbitrary strings and are the obvious-looking hack, but they are not a secret store:
not encrypted at rest by default, readable by realm administrators and by any mapper.

**Keycloak's real per-user credential store is identity brokering.** With *Store Token*
set on an upstream provider, Keycloak keeps that provider's access and refresh token per
user and returns it from `GET /realms/{realm}/broker/{alias}/token`, gated by the
`read-token` role. That matters here more than it first looks: for Gmail and Microsoft 365
the IMAP credential *is* an OAuth token (XOAUTH2 / OAUTHBEARER), so for those providers a
brokered token is the mail credential, and mailmind would never see a password at all. For
a plain IMAP server it holds nothing, because there is nothing to broker.

**Authelia holds nothing for us either way.** Its storage keeps its own authentication
data — preferences, 2FA device handles and secrets, WebAuthn credentials, identity
verification tokens — with column-level encryption. Its "secrets" documentation is about
supplying Authelia's *own* configuration secrets from files. And it cannot broker: it
implements the OpenID Connect Provider role and explicitly declines the Relying Party
role, so it never holds an upstream token it could hand over. Authelia is a front door,
not a keyring — which is the right thing for it to be, and the right thing to use it for.

**So the seam is `password_url`, and it already exists.** The configuration and the
`account` row both hold a URL saying where a password is found, never a password. Adding a
secret manager is adding a scheme:

| Scheme | Where it fits |
|---|---|
| `secret-storage://` | one person's own machine, desktop store |
| `file://` | headless: a file with a mode on it, systemd-creds, a mounted secret |
| `vault://` | shared: Vault, OpenBao, Infisical — not built |
| `oauth-broker://` | Gmail and Microsoft 365 via Keycloak's brokered token — not built |

Nothing above the configuration layer changes for any of them, which is the point of the
indirection and the reason it was worth having before there was a second scheme.

## Accounts are rows

Adding an account is a thing a person does in the review UI, not a thing they do by
editing a file and restarting a process. That decides something that was previously
ambiguous: **the `account` row is the source of truth, and the configuration file is seed
data for it.**

It was ambiguous in a way that did not work. Connections were built by taking the row's
name back to the configuration and looking it up there, so an account created any other
way existed and was unusable at the same time — which is every account a web form would
ever create. A connection is now built from the row's own columns. `mailmindctl bootstrap`
still seeds configured accounts into rows, and a file that names no accounts is a normal
thing rather than a broken one.

The form itself is not built. What it has to do beyond the obvious: choose *where* the
password lives rather than take a password, and probe the server before saving, so a typo
is caught at the form and not at three in the morning.

## Bundles are large when the action is one action

[03](03-review.md) asked what "see the effect" means for a suggestion touching two hundred
messages. The answer is that the number was never the thing — homogeneity is. One
operation and one target over an enumerated list is reviewable at a size that the same
list would not be if each item could do something different. A hundred messages moving to
Archive is one decision shown a hundred times; a hundred messages each doing their own
thing is a hundred decisions dressed as one.

So the limits are a guard against a bundle nobody can *render*, not against one nobody can
*understand*, and they belong to the deployment rather than to the design. They are
configurable under `[limits]` today. Whether they should also be per-tenant rows editable
in the UI is the same question as accounts, and gets the same answer if the answer to
accounts sticks.

## Meets

- [03](03-review.md) — the review step, and what makes a large bundle reviewable
- [07](07-tenancy.md) — one tenant here; a shared deployment is where that stops being free
- [10](10-running-it.md) — the local mode, from the outside

## Open questions

- Behind a proxy, the MCP endpoint's DNS-rebinding allow-list is built from the bind
  address and will not match the proxy's public Host. Does it need configuring, or does
  `/mcp` simply not belong on a shared deployment?
- If the proxy authenticates, how does the identity it asserts become a `producer` row and
  a tenant? A header the proxy sets is the usual answer and is only as good as the promise
  that nothing else can reach the port.
- Is `behind_auth_proxy` the right shape, or should the escape hatch name what is in front
  of it, so the configuration says something checkable rather than something asserted?
- A grant token is minted on the command line and printed once. On a shared deployment
  that is the wrong place — but "the review UI mints agent tokens" gives the UI a power the
  rest of the design is careful about.
- Does the account form ever hold a password long enough to write it into a secret store,
  or does it only ever accept a URL to one that is already there? The first is much nicer
  to use and puts a password through an HTTP request.
- Nothing here says what happens to `secret-storage://` when the service is not the
  session that unlocked the store — a daemon started at boot has no keyring to read.
