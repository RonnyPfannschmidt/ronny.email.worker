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

[Connecting a client](docs/connecting.md) is the spine: both transports, what a grant is,
and what to build into an agent. [Reviewing](docs/reviewing.md) is the other side of it, and
[the security model](docs/security-model.md) is what the arrangement promises and where that
stops. [Setting it up](docs/setup.md) points it at a mailbox of your own; the
[reference](docs/reference/mcp.md) covers the MCP surface, `mailmindctl` and the
configuration file, and is rendered from the code.

Why any of it is shaped that way is in [design notes](docs/design/index.md), starting with
[the intent](docs/design/01-intent.md).

The whole of it builds as a site: `uv run --group docs mkdocs serve`.

## A first look, costing you nothing

A disposable IMAP server seeded with the test corpus, so nothing here touches your own
mail. [Test drive](docs/test-drive.md) is this, and [Setting it up](docs/setup.md) is
pointing it at a real mailbox, including where the password should live.

```
podman run -d --rm --name mailmind-dev -p 3144:143 -e MAILNAME=example.org \
  -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
  docker.io/antespi/docker-imap-devel:latest
uv run dev/seed_mailbox.py

export MAILMIND_CONFIG=mailmind.dev.toml MAILMIND_DEV_PASSWORD=secret
uv run mailmindctl bootstrap && uv run mailmindctl probe && uv run mailmindctl sync
uv run mailmindctl grant --producer opencode   # a bearer token, printed once
uv run mailmindctl serve                       # a link with the login key in it
```

An MCP client can also spawn its own connection over a pipe: `mailmindctl mcp` speaks MCP
on stdin and stdout and tells the model where the review UI is — `--serve` brings one up
for the life of the session. [Connecting a client](docs/connecting.md)
covers pointing your own agent repository at either transport, and
[`integrations/`](integrations/) has ready-made client configurations.
