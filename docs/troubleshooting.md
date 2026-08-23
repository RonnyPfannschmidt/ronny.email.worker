# Troubleshooting

**"no configuration at … — it was named, so this is a mistake."** `--config` or
`MAILMIND_CONFIG` points at a file that is not there. Falling back would mean no accounts
and a database in whatever directory the process started in.

**The agent connects and sees no mail.** Usually the same thing one step later: it is
reading a different configuration, or a relative `database_url` resolved against a working
directory nobody chose. Set `MAILMIND_CONFIG` in the client's `env` block.

**A bad token that is not a bad token.** A POST to `/mcp` without the trailing slash is a
307 that some clients do not follow. Use `/mcp/`. The other one is the `Host` header:
rebinding protection allows loopback and nothing else.

**"refusing to listen on 0.0.0.0."** Working as intended —
[the security model](security-model.md#loopback).

**A 401, or a tab that stopped working.** A restart mints a new key. Back to the terminal:
`mailmindctl review --open`. If an *agent* reported the 401, that is the design.

**"no review UI has left a link at …"** Nothing is serving on that port. Start one, or name
the right port with `mailmindctl review --port` — a session that used `--serve --port 0`
took a free one.

**An account you never configured is in the list.** A bootstrap against a copy of the
example file seeds `personal` at `imap.example.org`, and taking it back out of the file does
not take it out of the database — the row is the source of truth. `mailmindctl account list`
then `mailmindctl account forget personal`.

**`probe` fails.** The account declares a capability the server does not offer. Fix the
declaration rather than working around the probe: it decides what the service attempts. The
twenty-odd it prints in the other direction are informational.

**`secret-storage://` raises on a server.** No session bus, so keyring resolves to a backend
that raises. Use `file://`. The same goes for a daemon started at boot: no unlocked keyring
to read.

**`secret-storage://` needs the keyring package.** `uv sync` installs it through the `dev`
group; otherwise `pip install -e '.[secrets]'`, or `uv tool install --with keyring .`.
