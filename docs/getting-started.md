# Getting started

From a clean checkout to a review UI with mail in it, without giving anything a password
that matters and without editing a configuration file.

The mailbox here is a disposable Dovecot in a container, seeded with the same adversarial
corpus the test suite uses. `mailmind.dev.toml` is checked in and points at it, so there
is nothing to copy and nothing to edit. Pointing at a real mailbox is
[Configuration](configuration.md), and worth doing second.

## What you need

Python 3.13 or newer, [uv](https://docs.astral.sh/uv/), and podman or docker. `uv run`
installs what it needs the first time you use it, so there is no install step.

## Stand up a mailbox

```
podman run -d --rm --name mailmind-dev -p 3144:143 -e MAILNAME=example.org \
  -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
  docker.io/antespi/docker-imap-devel:latest
uv run dev/seed_mailbox.py
```

The seeder talks IMAP directly rather than going through mailmind, and it lives in
`dev/` rather than in the package. That is deliberate: the shipped code has no way to
write mail into a mailbox other than applying something a person accepted, and a seeder
routed through it would have to widen that.

## Bring mailmind up

```
export MAILMIND_CONFIG=mailmind.dev.toml MAILMIND_DEV_PASSWORD=secret
uv run mailmindctl bootstrap && uv run mailmindctl probe && uv run mailmindctl sync
uv run mailmindctl grant --producer opencode   # prints a bearer token, once
uv run mailmindctl serve                       # prints a link with the login key in it
```

- `bootstrap` migrates the database and writes the configured account into a row.
- `probe` checks what the server offers against what the account declares.
- `sync` fills the local cache from the mailbox.
- `grant` mints a token for an agent connecting over HTTP. Over stdio you do not need one.
- `serve` runs the review UI and the MCP endpoint.

`serve` prints a URL with a key in it. Open it once: that trades the key for a session
cookie and drops it back out of the address bar. The key is the login, and it is the one
thing nothing connecting over MCP is ever told —
[Security model](security-model.md) has why that carries so much weight.

## What you should see

`sync` reports `INBOX: +6 ~0 -0`, and the cache holds the corpus with its special-use
folders recognised:

```
containers: Archive(None) Drafts(drafts) INBOX(None) Sent(sent) Trash(trash)
findings:   display_name_spoofs_address 1, first_contact 6, malformed_mime 1, no_message_id 1
```

`probe` reports the account `ok` and then lists twenty-seven capabilities Dovecot offers
that the account does not declare. That is informational and does not fail the probe; only
the other direction — declared and not offered — is loud, and only that one exits non-zero.

Propose a delete over MCP, accept it in the UI, and the message moves into the server's
own Trash, leaving five in INBOX. Nothing expunges: mail has no undo, and this is the one
place that could prove it.

## Point an agent at it

In another terminal, or from an MCP client's configuration:

```
uv run mailmindctl mcp --producer mail-agent
```

That speaks MCP on stdin and stdout — no port, no token — and tells the model where the
review UI is, so it can send you there. [Connecting an agent](agents.md) has the client
configurations for both transports.

## Take it all away

```
podman stop mailmind-dev
rm mailmind.dev.db*
```

The container goes, and the cache with it. Nothing else on your machine was touched.
