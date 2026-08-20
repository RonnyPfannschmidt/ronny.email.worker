# Security model

What mailmind promises, how it holds it up, and — the part worth reading twice — where it
stops.

## The promise

> Nothing reaches a mailbox through mailmind except an accept made by something holding
> the review key, and every accept is recorded against a producer.

Deliberately not "a person accepted it". If the thing holding the key is a person,
mailmind's boundary is the design's boundary. If your agent can read your terminal, it is
not, and no amount of checking inside this process changes that.

## What holds it up

**The agent surface has no apply.** Not a permission an agent lacks — `apply` is not a
value the capability enum can hold, and the module that writes to a mailbox is not
imported by anything on the agent side. The twelve tools are nine that look and three that
say; the full list is in [the MCP surface](reference/mcp.md).

**The review UI has a login.** A key, minted when the process starts and shown to whoever
started it. Following the link once trades it for an HttpOnly session cookie and drops the
key back out of the address bar. It is not a password: there is nobody here to have an
account, and a passphrase for a service on your own machine is friction protecting the
wrong thing. The key is not hard to guess and does not need to be. Everything rests on it
never being told to the agent.

**Every channel that reaches a model is checked for it.** The instructions at connect
time, tool results, resources, prompts, and the stderr an MCP client collects into a log.
The test suite asserts the key is absent from all five. `mailmindctl serve` prints the link
outright, because that is a command a person runs in their own terminal; `mailmindctl mcp
--serve` writes it to a file opened `O_CREAT` with mode `0600` and gives stderr only the
path, because some clients put their spawned process's log in front of the model.

**Changing something has to look like a person did it.** Every route that changes state
requires the [Fetch Metadata](https://developer.mozilla.org/en-US/docs/Glossary/Fetch_metadata_request_header)
headers a browser sends when somebody submits a form on a page it is showing —
`sec-fetch-mode: navigate`, `sec-fetch-dest: document`, `sec-fetch-site: same-origin`, and
a matching origin. Scripts are forbidden to set these, so inside a browser they cannot be
forged. A refusal writes a `ui_change_refused` event, because nothing changed but
something tried, and whoever owns the mail should be able to find that out. This was never
a boundary on its own — outside a browser the four headers are one line of code — and it
is a check that is only worth anything sitting behind the login.

`Sec-Fetch-User: ?1` would be the better signal and is deliberately not required: Safari
has never sent it, and a check that locks out a whole browser is a check somebody turns
off.

**Nothing is applied on a premise that stopped being true.** Freshness is checked before a
bundle is shown and again per item immediately before it is applied. The second gap is the
dangerous one, because a person has already said yes. A suggestion whose message moved,
whose flags changed, or whose folder was recreated is refused with what changed in words,
rather than quietly doing something else.

**Every accept is recorded against a producer.** Who proposed, who decided, and what was
refused; the record is the point of the `producer` row rather than authentication.

## Where it stops

It moves the bar from *any agent that can make an HTTP request* to *an agent that can read
the person's terminal or files*. That is a real step and it is the last one this process
can take by itself.

An agent with a shell on the same account does not need the review UI at all. It can read
`mailmind.toml`, resolve the mailbox password through the very indirection that keeps it
out of that file, and speak IMAP itself. At that point mailmind is not in the path and
nothing it does matters.

**The boundary that holds is how the agent is run** — a sandbox, a container, an account of
its own — and that is not something mailmind can draw for you. Everywhere a review UI comes
up, it says so in as many words.

## Serving it to anything but this machine

Refused. The session cookie is a bearer token travelling over plain HTTP, so anything that
can watch the wire can take it, replay it, and accept somebody's mail.

```
$ mailmindctl serve --host 0.0.0.0
Error: refusing to listen on 0.0.0.0: the review UI's session cookie is a bearer
token and this is plain HTTP, so anyone who can watch the wire can take it and
accept somebody's mail. …
```

Loopback means provably loopback — `127.0.0.0/8`, `::1`, `localhost`. A hostname is not
resolved to find out, because it could resolve to anything later and the check is meant to
be sure rather than accommodating.

The way past it is to put TLS and authentication in front and say so with
`behind_auth_proxy = true`: forward auth from a reverse proxy — Authelia, oauth2-proxy, an
identity-aware proxy. mailmind does not grow a user table, a password reset or a session
cookie of its own; a review UI that invented its own identity would be the weakest part of
a design whose whole argument is about who may change what.

Two things about that mode are unfinished, and named in
[11](design/11-deployment-and-identity.md): how a proxy-asserted identity becomes a
`producer` row, and that the MCP endpoint's DNS-rebinding allow-list is built from the bind
address and will not match a proxy's public Host.

## Untrusted content

Mail is written by strangers, and everything that reads it has to treat it as an attempt to
steer whoever is reading. The server says so in its instructions and marks every body it
returns with a warning. The agent is where it has to actually hold — `tests/corpus/` has a
message engineered to look exactly like an instruction, and one whose display name claims
an address it is not from. Point your agent at both early.

Findings the sync records — `first_contact`, `display_name_spoofs_address`,
`malformed_mime`, `no_message_id` — are observations shown to a reviewer, not a filter.
[08](design/08-untrusted-content.md) has the reasoning.

## What a grant is

The view an agent gets is given, not chosen. It cannot name a tenant, widen its own scope,
or assert who it is. `--capability` narrows what it may do; `--account` narrows what it may
see, and an account outside the grant reads as *absent* rather than forbidden, because that
is what it is from where the agent stands.

| Capability | Lets an agent |
|---|---|
| `observe` | read what is cached: accounts, folders, messages, bodies on request |
| `suggest` | propose a bundle, and withdraw its own |
| `assess` | record how trustworthy something looks, as interpretation |

There is no fourth. Start narrower than you think you need: an agent that reads and
proposes wants `observe` and `suggest`, and `assess` belongs to something that reads mail
in order to say how trustworthy it looks, which is
[argued](design/02-action-suggestions.md) not to be the same producer that then proposes
acting on it.

Over stdio, `--producer NAME` reuses that producer's existing grant, so minting a narrow
one and then connecting over a pipe gives you the narrow one. Only a producer with no grant
at all gets a full one minted for it, on the reasoning that whoever spawned the process
could read the database anyway. Nothing checks that the thing connecting is the agent
`--producer` names; over a pipe that is the same trust as the pipe itself.

## Threats this does not address

- Anything running as you, as above.
- A malicious *mailmind*, or a compromised dependency of it. The mailbox password is
  reachable from this process by design; that is what it is for.
- The mail server itself, and anyone who can read your IMAP traffic.
- A person accepting a bundle without reading it. The design can make a bundle honest and
  readable; it cannot make anybody read it.
