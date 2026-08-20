# ronny.email.worker

`mailmind` — an MCP server and webapp that lets agents work with someone's mailboxes,
without letting them change anything a person has not agreed to.

An agent connects over MCP, browses the mail its grant covers, and says what should happen
to it. It cannot make any of it happen: there is no tool that applies a change, no `apply`
value in the capability enum, and nothing on the agent side imports the code that writes to
a mailbox. A person reviews the proposed effect — every message, where it is, where it
would go — and accepts or rejects. Only then does the service touch the mailbox, and only
if nothing has moved in the meantime.

The first iteration is built: IMAP, one tenant, and enough to sort a long untended mailbox.
See [09 — Iteration one](docs/design/09-iteration-one.md) for what it does.

## Documentation

[Getting started](docs/getting-started.md) runs it against a mailbox that is not yours.
[Configuration](docs/configuration.md) points it at one that is, and decides where the
password lives. [Security model](docs/security-model.md) is what it promises and where that
stops. [Connecting an agent](docs/agents.md) is the other half, and
[`mailmindctl`](docs/reference/cli.md) and [the MCP surface](docs/reference/mcp.md) are the
lists to look things up in.

Why any of it is shaped that way is in [design notes](docs/design/index.md) — sketches and
arguments, starting with [the intent](docs/design/01-intent.md).

The whole of it builds as a site: `uv run --group docs mkdocs serve`.

## A first look, costing you nothing

A disposable IMAP server seeded with the test corpus, so nothing here touches your own
mail. [Getting started](docs/getting-started.md) covers this, and
[Configuration](docs/configuration.md) covers pointing it at a real mailbox, including
where the password should live.

```
podman run -d --rm --name mailmind-dev -p 3144:143 -e MAILNAME=example.org \
  -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
  docker.io/antespi/docker-imap-devel:latest
uv run dev/seed_mailbox.py

export MAILMIND_CONFIG=mailmind.dev.toml MAILMIND_DEV_PASSWORD=secret
uv run mailmindctl bootstrap && uv run mailmindctl probe && uv run mailmindctl sync
uv run mailmindctl grant --producer opencode   # prints a bearer token, once
uv run mailmindctl serve                       # prints a link with the login key in it
```

An MCP client can also spawn its own connection over a pipe: `mailmindctl mcp` speaks MCP
on stdin and stdout and tells the model where the review UI is — `--serve` brings one up
for the life of the session. [Connecting an agent](docs/agents.md)
covers pointing your own agent repository at either transport, and
[`integrations/`](integrations/) has ready-made client configurations.
