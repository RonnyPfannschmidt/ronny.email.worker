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
See [09 — Iteration one](docs/09-iteration-one.md) for what it does.

Start with [the intent](docs/01-intent.md); the whole design is in [docs/](docs/).

## A first look, costing you nothing

A disposable IMAP server seeded with the test corpus, so nothing here touches your own
mail. [10 — Running it](docs/10-running-it.md) covers this and pointing it at a real
mailbox, including where the password should live.

```
uv venv && uv pip install -e '.[dev]'

podman run -d --rm --name mailmind-dev -p 3144:143 -e MAILNAME=example.org \
  -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
  docker.io/antespi/docker-imap-devel:latest
python dev/seed_mailbox.py

export MAILMIND_CONFIG=mailmind.dev.toml MAILMIND_DEV_PASSWORD=secret
mailmindctl bootstrap && mailmindctl probe && mailmindctl sync
mailmindctl grant --producer opencode      # prints a bearer token, once
mailmindctl serve                          # prints a link with the login key in it
```

An MCP client can also spawn its own connection over a pipe: `mailmindctl mcp` speaks MCP
on stdin and stdout and tells the model where the review UI is — `--serve` brings one up
for the life of the session. [12 — An agent of your own](docs/12-an-agent-of-your-own.md)
covers pointing your own agent repository at either transport, and
[`integrations/`](integrations/) has ready-made client configurations.
