# Troubleshooting

## "no configuration at … — it was named, so this is a mistake"

`--config` or `MAILMIND_CONFIG` pointed at a file that is not there. mailmind refuses
rather than falling back, because falling back would give an empty configuration and a
database in whatever directory the process happened to start in. Check the path; `~` is
expanded, so that is not the problem.

## The agent connects and can see no mail

Almost always the same thing one step later: the process is reading a different
configuration, or a different database, from the one you bootstrapped and synced. Set
`MAILMIND_CONFIG` in the client's `env` block, and check `database_url` is not a relative
path being resolved against a working directory nobody chose.

## A bad token that is not a bad token

A POST to `/mcp` without the trailing slash is a 307. Some clients follow it, some do not,
and the failure looks like an authentication problem. Use `http://127.0.0.1:8765/mcp/`.

The other one is the `Host` header: DNS-rebinding protection allows loopback and nothing
else, so a request arriving with any other Host is refused regardless of its token.

## "refusing to listen on 0.0.0.0"

Working as intended. Why, and what `behind_auth_proxy = true` commits you to:
[the security model](security-model.md#serving-it-to-anything-but-this-machine).

## The review UI says 401, or an open tab stopped working

A restart mints a new key, and the old cookie is not honoured by the new process. Go back
to the terminal: `mailmindctl review --open`, or the link `serve` printed.

If an *agent* reported the 401, that is the design. It was not given the key.

## "no review UI has left a link at …"

Nothing is serving on that port, or the server that was is gone. Start one with
`mailmindctl serve`, or name the right port with `mailmindctl review --port`. A UI brought
up by `mailmindctl mcp --serve --port 0` took a free port, so `--port` is how you say which.

## `probe` fails

Declared and not offered: the account's `caps` claim something the server does not do.
Either the server changed, or the declaration was optimistic. Fix the declaration rather
than working around the probe — it decides what the service attempts, which is the whole
point of it being written down.

The twenty-odd capabilities it prints in the other direction are informational and do not
fail anything.

## `secret-storage://` raises on a server

No session bus, so keyring resolves to a backend that raises rather than one that stores
anything. Use `file://` — see [Configuration](configuration.md#file-headless). The same
applies to a daemon started at boot on a desktop machine: there is no unlocked keyring for
it to read.

## `secret-storage://` needs the keyring package

`uv sync` installs it through the `dev` group; a plain install needs the extra:
`pip install -e '.[secrets]'`.
