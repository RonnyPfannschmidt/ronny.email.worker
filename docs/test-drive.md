# Test drive

A disposable Dovecot, seeded with the corpus the tests use, so nothing here touches your own
mail. `mailmind.dev.toml` is checked in and points at it — nothing to copy, nothing to edit.

You need [uv](https://docs.astral.sh/uv/) and podman or docker. From a checkout:

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

Open the link once — that is the login, and it is the thing nothing connecting over MCP is
told.

## What you should see

`sync` reports `INBOX: +6 ~0 -0`, and the cache holds the corpus with its special-use
folders recognised:

```
containers: Archive(None) Drafts(drafts) INBOX(None) Sent(sent) Trash(trash)
findings:   display_name_spoofs_address 1, first_contact 6, malformed_mime 1, no_message_id 1
```

`probe` reports the account `ok`, then lists twenty-seven capabilities Dovecot offers that
the account does not declare. That direction is informational. The other one is loud.

Point a client at it with `uv run mailmindctl mcp --producer mail-agent`
([Connecting a client](connecting.md)), propose a delete, accept it in the UI: the message
lands in the server's own Trash and five are left in INBOX. Nothing expunges.

Six messages is not an untended mailbox, which is the honest limit of this corpus — whether
a bundle stays reviewable at hundreds is still an open question.

## Take it away

```
podman stop mailmind-dev
rm mailmind.dev.db*
```
