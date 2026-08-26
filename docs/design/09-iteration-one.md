# 09 — Iteration one

> `#tried`. What was built, what it cut, and what it found out.

The first thing built rather than sketched. Its goal is narrower than the design: make a
long untended IMAP mailbox sortable, with a local agent browsing it over MCP and a person
accepting the results in a small web UI.

## What it is

One process. `/mcp` is a streamable-HTTP MCP endpoint; everything else is the review UI.
SQLite behind both. `mailmindctl` migrates, seeds, probes, syncs and serves.

An agent gets twelve tools — nine that look, three that say — and six resources. There
is no tool that applies anything ([the security model](../security-model.md) has what
makes that structural); a test asserts the applier is imported nowhere on the agent side.

## What it cut

Gmail, follow-up and fetching apps, credential minting, drafts, sending, standing
authority, and any model inside the service. Tenancy is in the data model with tenant zero
as the only tenant.

Assessment is split as [02](02-action-suggestions.md) asks but only half is built: the
service computes **mechanical** findings itself — a display name naming a different
address, characters that do not render, a link whose text disagrees with its target,
unparseable MIME, a first-contact sender, an address in the body that is in no header — and
an agent can add **interpretation** findings over MCP. Nothing in the service calls a model.

## No model has been pointed at this

Worth stating plainly, because it is the thing the iteration was supposed to answer and
did not. The end-to-end run used a hardcoded script speaking MCP over HTTP — initialize,
`tools/list`, then a fixed sequence of calls written in advance. Every tool choice in it
was made by a person, not by a model.

So what has been demonstrated is that the surface *works*: the transport, the token, the
grant scoping, the bounded observation, the premise capture, the staleness refusal, the
apply path. Whether the surface is *usable* — [05](05-agent-surface.md)'s actual question,
what an agent needs to be useful — is untouched. Everything below about what an agent
would reach for is a guess that has not met one.

## What it found out

**The bundle survived as the reviewed unit.** One operation, one target, every message
enumerated with where it is and where it would go. Sorting the trial mailbox took two
bundles for 35 of 39 messages. The thing that makes this not-a-bulk-accept is that the
effect is a list you can read, and that is a property of homogeneity, not of size.
Whether it holds at 500 items is still unknown — the cap is a guess.

**`summarize_senders` is expected to be the tool the goal runs on — untested.** On an
untended mailbox the answer to "what is in here" starts with an aggregate: six rows
described the trial mailbox, where enumerating to learn the same thing would have hit the
observation limit. But the script called it because it was told to. Whether a model
reaches for it, or enumerates until it runs out of room, is exactly what has not been
tried.

**A capability you declare but do not implement is worse than one you lack.** Expunge
detection was skipped whenever the server offered QRESYNC — which is never implemented
here, so on a real Dovecot nothing ever noticed a message leaving a folder, and every
suggestion resting on a moved message stayed fresh. The fake did not offer QRESYNC, so the
whole suite was green. Only running against a real server found it. The declaration must
say what the service *does*, not what the server *can*.

**The second check earned its place immediately.** In the same run, one message had been
filed by hand and the cache had not noticed. The pre-show check passed on stale data; the
check at apply time asked the server and refused. Both checks exist because either can be
the one that catches it.

**Nothing yet says whether the record needs to be an event log.** [06](06-core.md)'s
question is not answered — a plain table with an append-only audit trail has not hurt, but
nothing has needed replay either.

## Still open, and now sharper

- **What an agent actually needs** ([05](05-agent-surface.md)). Nothing has been learnt
  here at all, because no model has connected. The next thing to do is point a real agent
  at it and watch which tools it reaches for and where it runs out of view — including
  whether it can form a sensible bundle without the bodies, and what it does when told a
  result was truncated.
- **Rubber-stamping** ([03](03-review.md)) is untested. Two bundles is not a queue fifty
  deep, and both were dispatched by the person who wrote the proposal script. This remains
  the question the rest of the design may have to serve.
- **Assessment from the producer.** With one agent, the assessor and the producer are the
  same thing. The schema records both and the review page says so in a banner, but the
  rule of 02 is stated, not enforced. A second producer identity is what would enforce it.
- **Tenancy enforcement** is application-level: SQLAlchemy's `with_loader_criteria` recipe
  over every ORM statement, so a query that forgets its filter returns nothing. That is not
  *below* the code that queries, as [07](07-tenancy.md) wants. Postgres row-level security
  and one SQLite file per tenant are both still open; `sqlalchemy-tenants` does the first
  but is Postgres-only.
- **Bodies** are cached lazily and evictably, and `cache_bodies = false` keeps them out
  entirely. [04](04-mailbox-access.md)'s liability question is left answerable by a setting.

## Running it

[10 — Running it](10-running-it.md) has the whole of it, including a throwaway mailbox to
point at so a first look costs nobody their own mail.

Tests run against an in-process fake by default. The container tier needs a real server:

```
podman run -d --rm -p 3144:143 -e MAILNAME=example.org \
  -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
  docker.io/antespi/docker-imap-devel:latest
MAILMIND_IMAP_TARGET=127.0.0.1:3144 MAILMIND_TEST_PASSWORD=secret \
  MAILMIND_IMAP_CAPS="CONDSTORE,QRESYNC,MOVE,UIDPLUS,SPECIAL-USE,IDLE" \
  uv run pytest tests/test_container_imap.py -m ""
```

`MAILMIND_IMAP_CAPS` is the declared matrix for that target. Tests skip on a *declared*
absence and never on a probed one, so a server that quietly stops offering CONDSTORE turns
the suite red rather than green. GreenMail works too and declares no CONDSTORE, which is
what makes it useful: it catches assumptions Dovecot would let through.
