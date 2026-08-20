# Configuration

One TOML file. It declares accounts, where the review UI listens, and the limits a
deployment puts on what a single request or bundle may be. What it never holds is a
password — only a URL saying where one is found.

## Where the file is found

In order:

1. `--config PATH`, or `MAILMIND_CONFIG` in the environment. `~` is expanded, because
   these get written into MCP client configurations by hand where no shell expands it.
2. `./mailmind.toml`, if it exists.
3. `$XDG_CONFIG_HOME/mailmind/mailmind.toml`, which is `~/.config/mailmind/mailmind.toml`.

A configuration that was *named* and is not there is an error. A default that is not there
is a fresh install, and loads as an empty configuration. The difference matters for a
process an MCP client spawned: it inherits a working directory nobody chose, so a silent
fallback would give it no accounts and a database wherever it happened to start.

Set `MAILMIND_CONFIG` explicitly for anything spawned by a client.

## The file

```toml
database_url = "sqlite:///mailmind.db"

bind = "127.0.0.1"
port = 8765
# behind_auth_proxy = false

[limits]
max_messages_per_request = 200
bundle_expiry_days = 7
body_cache_bytes = 268435456
max_bundle_size = 500

[accounts.personal]
host = "imap.example.org"
port = 993
use_ssl = true
caps = ["CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE"]
cache_bodies = true

[accounts.personal.login]
username = "me@example.org"
password = "secret-storage://imap.example.org"
```

`mailmind.toml.example` in the repository is this file with the reasoning written into it.

### Top level

| Key | Default | Means |
|---|---|---|
| `database_url` | `sqlite:///mailmind.db` | SQLAlchemy URL; SQLite is what is tested. |
| `bind` | `127.0.0.1` | Refuses to be anything but loopback unless `behind_auth_proxy`. |
| `port` | `8765` | Both processes read this file, which keeps the advertised address honest. |
| `behind_auth_proxy` | `false` | An assertion that something in front authenticates. |

`behind_auth_proxy` is not a feature. Nothing in the process can check that anything is in
front of it, which is exactly why it has to be written down rather than inferred from the
bind address. [Security model](security-model.md#serving-it-to-anything-but-this-machine)
has what you are taking responsibility for.

### `[limits]`

| Key | Default | Means |
|---|---|---|
| `max_messages_per_request` | `200` | A request matching more returns fewer and says so. |
| `bundle_expiry_days` | `7` | Suggestions nobody decides expire rather than accumulate. |
| `body_cache_bytes` | `268435456` | Cached body text kept before the coldest is evicted. |
| `max_bundle_size` | `500` | A bundle larger than this is refused at the proposal. |

These guard against a bundle nobody can *render*, not one nobody can *understand* — see
[Reviewing](reviewing.md#why-a-two-hundred-message-bundle-is-fine).

### `[accounts.NAME]`

| Key | Default | Means |
|---|---|---|
| `host` | required | IMAP host. |
| `port` | `993` | |
| `use_ssl` | `true` | |
| `caps` | see below | What this server is *declared* to be able to do. |
| `cache_bodies` | `true` | `false` keeps message bodies out of the local database entirely. |
| `login.username` | required | |
| `login.password` | required | A URL, never a password. |

The default `caps` are `CONDSTORE`, `MOVE`, `UIDPLUS`, `SPECIAL-USE` and `IDLE`.

The declaration decides what the service attempts; `mailmindctl probe` checks it against
the server and is loud when the server does not offer something declared. It never quietly
downgrades — drop `CONDSTORE` and conditional applies become best-effort ones that say so.
`QRESYNC` is deliberately absent from the default: nothing here uses it, so demanding it
would fail a probe for no gain.

Accounts in this file are **seed data**. `mailmindctl bootstrap` writes them into `account`
rows, and a connection is built from the row — so an account added another way works
without ever being named here, and a file that names no accounts is a normal thing.

## Where the password lives

The scheme decides. Which one is right depends on something the service cannot know, so it
is a decision you make rather than one defaulted to silently. An unknown scheme is refused
when the configuration loads, not when a connection is attempted at three in the morning.

### `secret-storage://` — at a desk

The desktop secret store through [keyring](https://pypi.org/project/keyring/):
SecretService (gnome-keyring, KWallet) on Linux, Keychain on macOS, Credential Locker on
Windows. Stored once, by you, outside the repository, and read at connect time.

```
keyring set imap.example.org me@example.org
# password = "secret-storage://imap.example.org"
```

The service key alone is enough: with no `/user` part the login's own username is the
entry, so the common case reads as the host and no more. It needs the `secrets` extra
(`pip install -e '.[secrets]'`, or a plain `uv sync`, whose `dev` group pulls it in) and a
session bus to talk to.

### `file://` — headless

A container, a bare SSH session, CI: no session bus, so keyring resolves to a backend that
raises rather than one that stores anything. There is no fixing that from here — a secret
store you can read without a session is a file with a mode on it.

```
install -m 600 /dev/stdin ~/.secrets/mailmind-personal <<< "$PASSWORD"
# password = "file://~/.secrets/mailmind-personal"
```

Everything after the scheme is the path, so `file:///abs`, `file://~/rel` and `file://rel`
all mean what they look like. `systemd-creds` and a mounted secret land in the same place:
something at a path, readable by this process and not by others.

### `env://` — neither

It exists, and `mailmind.dev.toml` uses it to hold the word `secret` for a throwaway
container. Anywhere else it means the password is in your shell history and in the
environment of every process you launch from that shell, including the agent.

### Not built

`vault://` for Vault, OpenBao or Infisical, and `oauth-broker://` for Gmail and Microsoft
365, where the IMAP credential *is* an OAuth token and an identity provider that brokers
one is holding the mail credential already. Adding a secret manager is adding a scheme;
nothing above the configuration layer changes.
[11](design/11-deployment-and-identity.md) has why an identity provider is not otherwise a
place to put a mailbox password.
