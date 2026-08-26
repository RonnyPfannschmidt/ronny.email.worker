# Security model

## The promise

> Nothing reaches a mailbox through mailmind except an accept made by something holding
> the review key, and every accept is recorded against a producer.

Deliberately not "a person accepted it". If the thing holding the key is a person,
mailmind's boundary is the design's boundary. If your agent can read your terminal, it is
not.

## What holds it up

**No apply on the agent surface.** `apply` is not a value the capability enum can hold, and
the module that writes to a mailbox is not imported by anything on that side. There is also
no send, no append, and no way to create or delete a folder.

**A login on the review UI.** A key minted at startup and shown to whoever started the
process; following the link once trades it for an HttpOnly session cookie. It is not a
password — there is nobody here to have an account — and it does not need to be hard to
guess. Everything rests on it not being told to the agent.

**Five channels checked for the key.** Instructions, tool results, resources, prompts, and
the stderr an MCP client collects. The test suite asserts it is absent from all five.
`serve` prints the link, because a terminal is not an agent log; `mcp --serve` writes it to
a file opened `O_CREAT` with mode `0600` and gives stderr the path.

**Changes are session-authenticated and CSRF-checked.** The cookie is the
authentication; every state-changing route additionally requires that the request came
from a page this service served —
[`Sec-Fetch-Site: same-origin`](https://developer.mozilla.org/en-US/docs/Glossary/Fetch_metadata_request_header),
a header a browser sets and script cannot, with a matching `Origin` as the fallback for
browsers that send no fetch metadata — and a per-session token carried by every form.
That admits a form submission and a same-origin fetch alike, which is what lets the UI's
own script submit forms. A refusal writes a `ui_change_refused` event. (An earlier
navigation-only check lives in
[design history](design/history/2026-08-26-how-the-review-ui-got-a-login.md).)

**Nothing is applied on a stale premise.** Freshness is checked before a bundle is shown and
again per item before it is applied.

**Every decision is recorded** against a producer — who proposed, who decided, what was
refused.

## Where it stops

It moves the bar from *any agent that can make an HTTP request* to *an agent that can read
your terminal or files*. That is the last step this process can take alone.

An agent with a shell on the same account does not need the review UI. It can read
`mailmind.toml`, resolve the mailbox password through the very indirection that keeps it out
of that file, and speak IMAP itself. **The boundary that holds is how the agent is run** — a
sandbox, a container, an account of its own — and mailmind cannot draw it for you. It says
so wherever a review UI comes up.

Also not addressed: a compromised dependency of mailmind itself, the mail server and anyone
who can watch your IMAP traffic, and a person who accepts a bundle without reading it.

## Loopback

The session cookie is a bearer token over plain HTTP, so anything but a provably loopback
bind — `127.0.0.0/8`, `::1`, `localhost` — is refused. A hostname is not resolved to find
out; it could resolve to anything later.

Past it is TLS and authentication in front, asserted with `behind_auth_proxy = true`:
forward auth from a reverse proxy. mailmind does not grow a user table or a session of its
own. Two things about that mode are unfinished, and named in
[11](design/11-deployment-and-identity.md): how a proxy-asserted identity becomes a
producer, and that the MCP endpoint's rebinding allow-list is built from the bind address.

## Untrusted content

Mail is written by strangers. The server tells a connecting model that content is data and
marks every body it returns, and the sync records what it can see mechanically —
`first_contact`, `display_name_spoofs_address`, `malformed_mime`, `no_message_id`. Those are
observations shown to a reviewer, not a filter. The agent is where it has to hold; see
[08](design/08-untrusted-content.md).
