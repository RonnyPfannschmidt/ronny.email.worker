# Configuration

One TOML file. It never holds a password — only a URL saying where one is found.
`mailmind.toml.example` is this file with the reasoning written into it.

## Where it is found

`--config PATH` or `MAILMIND_CONFIG` (with `~` expanded), else `./mailmind.toml`, else
`$XDG_CONFIG_HOME/mailmind/mailmind.toml`.

A configuration that was *named* and is missing is an error; a default that is missing is a
fresh install. Set `MAILMIND_CONFIG` for anything an MCP client spawns, which inherits a
working directory nobody chose.

## Keys

```toml
database_url = "sqlite:///mailmind.db"
bind = "127.0.0.1"
port = 8765
# behind_auth_proxy = false
```

| Key | Default | |
|---|---|---|
| `database_url` | `sqlite:///mailmind.db` | SQLAlchemy URL; SQLite is what is tested. |
| `bind` | `127.0.0.1` | Must be loopback unless `behind_auth_proxy`. |
| `port` | `8765` | Both processes read this, which keeps the advertised address honest. |
| `behind_auth_proxy` | `false` | Asserts that something in front authenticates. |

`behind_auth_proxy` cannot be checked from inside the process, which is why it is asserted
rather than inferred — [what it commits you to](../security-model.md#loopback).

```toml
[limits]
max_messages_per_request = 200
bundle_expiry_days = 7
body_cache_bytes = 268435456
max_bundle_size = 500
```

| Key | Default | |
|---|---|---|
| `max_messages_per_request` | `200` | A request matching more returns fewer, and says so. |
| `bundle_expiry_days` | `7` | Undecided bundles expire rather than accumulate. |
| `body_cache_bytes` | `268435456` | Cached body text kept before the coldest is evicted. |
| `max_bundle_size` | `500` | A larger bundle is refused at the proposal. |

```toml
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

`caps` is what the server is *declared* to do, and it decides what the service attempts.
`mailmindctl probe` checks it and never quietly downgrades: drop `CONDSTORE` and conditional
applies become best-effort ones that say so. `QRESYNC` is absent because nothing here uses
it. `cache_bodies = false` keeps message bodies out of the local database entirely.

Accounts here are seed data: `bootstrap` writes them into `account` rows, and a connection
is built from the row, so an account added another way works without being named here.

## Where the password lives

| Scheme | For |
|---|---|
| `secret-storage://service[/user]` | A desktop session. Needs `keyring` and a session bus. |
| `pass://entry/name` | A [password-store](https://www.passwordstore.org) you already keep. |
| `file://path` | Headless. A file with a mode on it; `systemd-creds`, a mounted secret. |
| `env://NAME` | A throwaway container, and nothing else. |

With no `/user`, `secret-storage://` uses the login's own username, so
`keyring set imap.example.org me@example.org` matches
`secret-storage://imap.example.org`. Everything after `file://` is the path, so
`file:///abs`, `file://~/rel` and `file://rel` all mean what they look like.

`pass://` runs `pass show` and takes **the first line**, which is the convention the whole
password-store ecosystem is built on: line one is the password, everything after it is
notes. The trailing newline goes and nothing else does, so a password ending in a space
survives. mailmind never touches the store itself — no gpg, no decryption, no
`PASSWORD_STORE_DIR` — it asks the command and reads a line. If gpg needs a passphrase and
nothing can prompt for one, the call gives up after a minute and says so rather than
hanging a sync forever.

`env://` puts the password in your shell history and in the environment of every process you
launch from that shell, the agent included.

An unknown scheme is refused when the configuration loads, not when a connection is
attempted. `vault://` and `oauth-broker://` are sketched and not built —
[11](../design/11-deployment-and-identity.md) has why the indirection is the seam.
