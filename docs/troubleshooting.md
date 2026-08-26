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

**An account you never configured is in the list.** Seeding from a copy of the
example file seeds `personal` at `imap.example.org`, and taking it back out of the file does
not take it out of the database — the row is the source of truth. `mailmindctl account list`
then `mailmindctl account forget personal`.

**Most of the mailbox says `partial`, and everything claims an attachment.** A cache
filled before August 2026 flagged every multipart message — most mail — because a sync
reads headers, and a multipart without its body looks truncated and looks like one
attachment of type `multipart/mixed`. Fixed at the source, and a migration undoes it in
place: no re-download, because the combination of both flags with no cached body is
diagnostic of the bug rather than of the mail.

**"this database is at 0002choice and this build needs 0007task".** The code moved on
and the database did not — ordinary when mailmind runs from a checkout that `git pull`
updates. `mailmindctl migrate` brings it up; nothing else does, so that no command
quietly rewrites a mail cache the first time it runs. Stop the service first if one is
running. `serve` in this state holds its port with a 503 page saying the same thing and
exits once the migration has run, so a supervisor restart picks up the new build.

**`probe` fails.** The account declares a capability the server does not offer. Fix the
declaration rather than working around the probe: it decides what the service attempts. The
twenty-odd it prints in the other direction are informational.

**`secret-storage://` raises on a server.** No session bus, so keyring resolves to a backend
that raises. Use `file://`. The same goes for a daemon started at boot: no unlocked keyring
to read.

**`pass://` hangs, or gives up after a minute.** gpg is waiting for a passphrase and
nothing can prompt for one — a service started at boot has no pinentry and no tty. Either
give the agent a cached passphrase before the service starts, or use `file://` there.

**`secret-storage://` needs the keyring package.** `uv sync` installs it through the `dev`
group; otherwise `pip install -e '.[secrets]'`, or `uv tool install --with keyring .`.
