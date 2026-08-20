# 10 — Running it

> `#sketch` `#open-questions`. Getting a mailmind up on your own machine, and deciding
> where the password lives.

Every other document here is written from the inside — what a component is made of, who
may do what to it. This one is written from outside, because the first thing anybody does
with mailmind is try to run it, and until now the answer was five lines of shell
duplicated in two files.

Serving it to anything but this machine is refused, because the review UI has no login —
[11](11-deployment-and-identity.md) has that bargain and the way out of it.

## Who this is for

Two people, who turn out to want the same thing:

- Someone **developing mailmind**, who needs a mailbox to point it at and does not want
  that mailbox to be their own.
- Someone **deciding whether to trust it**, who wants to watch it propose something and
  watch themselves reject it before it goes anywhere near real mail.

Both want to see the review UI with mail in it, having handed over nothing that matters.
That is the story this document is about:

> From a clean checkout, a mailmind serving a review UI with mail in it, without giving it
> a password that matters and without editing a configuration file.

## Two mailboxes

**A throwaway one.** A disposable Dovecot in a container, seeded with the adversarial
corpus the tests use. `mailmind.dev.toml` is checked in and points at it, so there is
nothing to copy and nothing to edit. This is the one to start with.

```
podman run -d --rm --name mailmind-dev -p 3144:143 -e MAILNAME=example.org \
  -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
  docker.io/antespi/docker-imap-devel:latest
python dev/seed_mailbox.py

export MAILMIND_CONFIG=mailmind.dev.toml MAILMIND_DEV_PASSWORD=secret
mailmindctl bootstrap && mailmindctl probe && mailmindctl sync
mailmindctl grant --producer opencode      # prints a bearer token, once
mailmindctl serve                          # UI on /, MCP on /mcp/
```

`podman stop mailmind-dev` takes it all away; `rm mailmind.dev.db*` takes the cache with
it.

**Your own.** `cp mailmind.toml.example mailmind.toml`, edit the host and the username,
and then the password question below stops being hypothetical. Everything else is the
same. Note that `mailmind.toml` is gitignored and `mailmind.dev.toml` is not, which is the
whole difference between them: one names a real mailbox and one names a container.

## Or let the client start it

`mailmindctl serve` is the version you run yourself. The other shape is one an MCP client
starts for you:

```
mailmindctl mcp --producer mail-agent --port 0
```

That speaks MCP on stdin and stdout — no port, no token — and brings the review UI up
beside it on a free port, telling the model where it is so the agent can pass the link on.
[12](12-an-agent-of-your-own.md) has the client configuration.

## Where the password lives

The configuration never holds a password. It holds a URL saying where to find one, and the
scheme decides. Which scheme is right depends on something the service cannot know, so it
is written down here rather than defaulted to silently.

**`secret-storage://` — at a desk.** The desktop secret store, via
[keyring](https://pypi.org/project/keyring/): SecretService (gnome-keyring, KWallet) on
Linux, Keychain on macOS, Credential Locker on Windows. The password is stored once, by
you, outside the repository, and mailmind reads it at connect time.

```
keyring set imap.example.org me@example.org
# password = "secret-storage://imap.example.org"
```

The service key alone is enough — `secret-storage://imap.example.org` uses the login's own
username as the entry, so the common case reads as the host and no more.

This is optional and now installable: `pip install -e '.[secrets]'`, or `.[dev]`, which
pulls it in. It had been documented and unreachable — `keyring` was in no dependency list
at all, and the test for the scheme injects a fake module into `sys.modules`, so nothing
noticed the package it names was never installable.

**`file://` — headless.** A container, a bare SSH session, CI: no session bus, so keyring
resolves to a backend that raises rather than one that stores anything. This is not a
mailmind limitation and there is no fixing it from here — a secret store you can read
without a session is a file with a mode on it.

```
install -m 600 /dev/stdin ~/.secrets/mailmind-personal <<< "$PASSWORD"
# password = "file://~/.secrets/mailmind-personal"
```

`systemd-creds` and a mounted secret both land in the same place: something at a path,
readable by this process and not by others.

**`env://` — neither.** It exists, it is what `mailmind.dev.toml` uses, and that file is
pointing at a container whose password is the word `secret`. Anywhere else it means the
password is in your shell history and in the environment of every process you launch from
that shell, including the agent. It is the wrong default and it is no longer the one in
the example.

Two alternatives were looked at and not taken, recorded so the next person does not have
to look them up: `keyrings.alt` adds encrypted file-backed keyring backends, and
`keyring_pass` bridges keyring to `pass`. Either would let one scheme cover both cases, at
the cost of a dependency that stores secrets itself rather than delegating to something
the operating system already audits. `file://` already covers the headless case without
that, so neither is worth the surface yet.

## Seeding is outside the package on purpose

`dev/seed_mailbox.py` talks to IMAP directly rather than through
[`MailBackend`](04-mailbox-access.md), and it is a script in the repository rather than a
`mailmindctl` subcommand.

That is not tidiness. [04](04-mailbox-access.md) puts writing mail outside the backend
surface — there is no send and no append, and the one thing the shipped code may do to a
mailbox is apply a suggestion a person accepted. A seeder that went through the protocol
would have to widen it, and the installed artifact would then contain a way to write mail
that nobody reviewed. So the corpus goes in the way another mail client would: a second
connection, from outside, exactly as the container tier's `out_of_band` fixture does.

## What you should see

Against the container above, `mailmindctl sync` reports `INBOX: +6 ~0 -0` and the cache
holds the corpus with its special-use folders recognised:

```
containers: Archive(None) Drafts(drafts) INBOX(None) Sent(sent) Trash(trash)
findings:   display_name_spoofs_address 1, first_contact 6, malformed_mime 1, no_message_id 1
```

`mailmindctl probe` reports the account `ok` and then lists twenty-seven capabilities
Dovecot offers that the account does not declare. That is informational and does not fail
the probe; only the other direction — declared and not offered — is loud, and only that
one sets a non-zero exit.

Proposing a delete over MCP and accepting it in the UI moves the message into the server's
own Trash and leaves five in INBOX. Nothing expunges: [01](01-intent.md) says mail has no
undo, and this is the one place that could prove it.

## Meets

- [04](04-mailbox-access.md) for the declaration: the dev config declares exactly the five
  capabilities the service attempts, and deliberately not QRESYNC, which Dovecot offers
  and mailmind does not use.
- [07](07-tenancy.md) for tenant zero, which the bootstrap creates and this story never
  has to mention.
- [09](09-iteration-one.md) for the container tier, which starts the same image for the
  test suite and currently starts its own.

## Open questions

- ~~Should `mailmindctl account add` exist?~~ Answered: adding an account is a thing you
  do in the review UI, so the `account` row is the source of truth and this file is seed
  data for it. See [11](11-deployment-and-identity.md). The form itself is not built.
- Is a checked-in `mailmind.dev.toml` a convenience or a footgun? It is one edit away from
  somebody committing their own host, and the thing protecting them is that a different
  filename is gitignored.
- Six messages is not an untended mailbox. [09](09-iteration-one.md)'s real question —
  whether a bundle stays reviewable at hundreds — needs a corpus this seeder cannot supply
  by hand. Generate one, or capture a real one?
- The container tier and this loop start the same image separately. One fixture, or is
  sharing it how the suite ends up depending on a running container?
- `probe` printing twenty-seven undeclared capabilities is noise on every run. Is the
  offered-but-not-declared direction worth reporting at all, or only worth reporting once?
- Still nobody has pointed a model at this. The loop above was driven by a script again,
  which is [09](09-iteration-one.md)'s finding repeated rather than answered — but at
  least now the thing a model would be pointed at can be stood up in one command.
