# 10 — Running it

> `#sketch` `#open-questions`. Getting a mailmind up on your own machine, and deciding
> where the password lives.

Every other document here is written from the inside — what a component is made of, who
may do what to it. This one is written from outside, because the first thing anybody does
with mailmind is try to run it.

The instructions themselves now live in the guide — [Getting started](../test-drive.md)
and [Configuration](../reference/configuration.md). What is left here is why they say what they say.

Serving it to anything but this machine is refused, because its session cookie travels
over plain HTTP —
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

The commands are in [Getting started](../test-drive.md); six of them, and none of
them edits a file.

The review UI has a login: `serve` prints a URL carrying a key, and following it once
trades that key for a session cookie. It is not a password and does not say who you are —
it says somebody may come in. What it is for is that the key is never given to an agent,
so something connecting over MCP can send you to the review UI and cannot open it itself.
[11](11-deployment-and-identity.md) has the reasoning, [12](12-an-agent-of-your-own.md)
has how far it holds.

`podman stop mailmind-dev` takes it all away; `rm mailmind.dev.db*` takes the cache with
it.

**Your own.** `cp mailmind.toml.example mailmind.toml`, edit the host and the username,
and then the password question below stops being hypothetical. Everything else is the
same. Note that `mailmind.toml` is gitignored and `mailmind.dev.toml` is not, which is the
whole difference between them: one names a real mailbox and one names a container.

## Letting a client start the agent side

`mailmindctl serve` is the review UI, and you run it. Beside it, an MCP client can spawn
its own connection over a pipe:

```
mailmindctl mcp --producer mail-agent
```

That speaks MCP on stdin and stdout — no port, no token. It reads the same configuration,
so it knows where `serve` is and tells the model, at connect time and on every bundle, so
the agent can pass the link on.

Add `--serve` and it brings the review UI up itself for the life of the session instead,
which is the whole of the setup when the agent is the only thing that ever proposes
anything. [12](12-an-agent-of-your-own.md) has the client configuration for both.

## Where the password lives

The configuration never holds a password. It holds a URL saying where to find one, and the
scheme decides. Which scheme is right depends on something the service cannot know, so it
is a decision written down rather than defaulted to silently. The recipes are in
[Configuration](../reference/configuration.md#where-the-password-lives); the reasoning is this:

**`secret-storage://` is for a desk.** The password is stored once, by you, outside the
repository, by something the operating system already audits. It needs a session bus to
talk to, and on a headless box there is nothing to talk to.

**`file://` is for headless, and is not a fallback.** Without a session, keyring resolves
to a backend that raises rather than one that stores anything. This is not a mailmind
limitation and there is no fixing it from here — a secret store you can read without a
session is a file with a mode on it. `systemd-creds` and a mounted secret land in the same
place.

**`env://` is for a container whose password is the word `secret`.** Anywhere else it means
the password is in your shell history and in the environment of every process you launch
from that shell, including the agent. It is the wrong default and it is no longer the one
in the example.

Two alternatives were looked at and not taken, recorded so the next person does not have
to look them up: `keyrings.alt` adds encrypted file-backed keyring backends, and
`keyring_pass` bridges keyring to `pass`. Either would let one scheme cover both cases, at
the cost of a dependency that stores secrets itself rather than delegating to something
the operating system already audits. `file://` already covers the headless case without
that, so neither is worth the surface yet.

`keyring` itself had been documented and unreachable — it was in no dependency list at all,
and the test for the scheme injects a fake module into `sys.modules`, so nothing noticed
the package it names was never installable. It is now an extra, and in the `dev` group.

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

Recorded in [Getting started](../test-drive.md#what-you-should-see), because it is the
thing somebody checks their own run against: six messages, the special-use folders
recognised, four findings, and a probe that is loud in one direction and quiet in the
other.

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
