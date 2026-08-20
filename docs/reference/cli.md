# `mailmindctl`

```
mailmindctl [--config PATH] COMMAND [OPTIONS]
```

`--config` names the configuration file; `MAILMIND_CONFIG` does the same from the
environment. [Configuration](../configuration.md) has where it is looked for otherwise, and
why a *named* file that is missing is an error.

## `bootstrap`

Migrate the database to head and write the configured accounts into `account` rows,
capabilities included. Run it once, and again after adding an account to the file. Existing
rows are left alone: the row is the source of truth, and the file is seed data for it.

## `probe`

Check every account's declared capabilities against what the server actually offers.

Declared and not offered is loud and exits non-zero — that is the direction where the
service would attempt something the server cannot do. Offered and not declared is printed
and ignored.

## `sync [--account NAME]`

Bring the local cache into step with the mailboxes, and report per folder as `+added
~changed -removed`. Without `--account`, every account.

## `status`

What is waiting for a person, without opening a browser.

## `serve [--host HOST] [--port PORT]`

Run the review UI and the MCP endpoint, and print the review link — key included, because
this is a command a person runs in their own terminal.

Refuses any bind address that is not provably loopback unless `behind_auth_proxy = true`
says something in front is doing TLS and authentication. See
[the security model](../security-model.md#serving-it-to-anything-but-this-machine).

## `review [--open] [--port PORT]`

Print the link that opens the review UI, or follow it in a browser with `--open`. `--port`
picks between servers when several are up.

The link is read from a file rather than from a terminal, because that is where
`mailmindctl mcp --serve` leaves it. If no server has left one, that is what the error
says.

## `grant [--producer NAME] [--capability CAP]... [--account NAME]...`

Mint a bearer token for an agent connecting over HTTP. Printed once; only its hash is
stored.

| Option | Default | |
|---|---|---|
| `--producer` | `opencode` | The agent this is for; what a decision is recorded against. |
| `--capability` | all three | `observe`, `suggest`, `assess`. Repeatable. No `apply`. |
| `--account` | all accounts | Repeatable. An account outside the grant reads as absent. |

## `mcp [OPTIONS]`

Speak MCP on stdin and stdout, which is the shape an MCP client expects: it spawns the
process and talks down a pipe, so there is no port to configure and no token to paste.

| Option | Default | |
|---|---|---|
| `--producer` | `local` | Whose proposals these are. Reuses that producer's grant. |
| `--token` | `MAILMIND_TOKEN` | Use an existing grant token, as the HTTP endpoint would. |
| `--serve` | off | Bring the review UI up too, for as long as this session lasts. |
| `--port` | configured | With `--serve`: which port to take. `0` picks a free one. |
| `--review-url` | configured | Where the review UI already is, if not where the config says. |

The review link is never printed to stderr here. It goes to a file with a mode on it under
`$XDG_RUNTIME_DIR`, and stderr gets the path — MCP clients collect stderr into a log, and
some put that log in front of the model.
