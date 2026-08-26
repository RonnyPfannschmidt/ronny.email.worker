# Running a checkout

For the latest rather than the last install, or because you are changing the code. Every
shape here runs what is in the checkout, so `git pull` is the upgrade.

`git pull` on a checkout that a service is running from changes the code under it. The
running process is unaffected — its code is already loaded — and nothing here ever
migrates on its own. What happens around a restart:

- **`mailmindctl serve` started against a database that is behind** does not crash: it
  holds the port with a 503 page naming both revisions and `mailmindctl migrate`, and
  exits cleanly once the migration has run. Under `Restart=always` (systemd, podman)
  that means a restart into a moved-on checkout shows a page saying what to do instead
  of a crash loop nobody can see, and comes back as the real service by itself after
  you migrate.
- **A migration run while a service is live** is noticed within a minute: the service
  stops working the queue and answers every request 503 with what happened, until it is
  restarted. Still: stop the service first when you can.
- **Every other command** refuses until the database catches up, and says so — which is
  better than the `no such column` traceback that taught us to add the check.

Restarting on code change stays the supervisor's job — `systemctl restart` after a
pull, a systemd path unit, or `watchfiles 'uv run mailmindctl serve' src/` for a dev
loop. mailmind's part is that no restart ever lands you in a crash loop or a quiet
mismatch.

**One-off, installing nothing:**

```
uvx --from ~/src/ronny.email.worker mailmindctl status
```

**A client that spawns it** — point the command at the checkout instead of at a
`mailmindctl` on PATH:

```json
{
  "mcpServers": {
    "mailmind": {
      "command": "uv",
      "args": ["run", "--directory", "/home/you/src/ronny.email.worker",
               "mailmindctl", "mcp", "--producer", "mail-agent", "--serve", "--port", "0"],
      "env": { "MAILMIND_CONFIG": "/home/you/.config/mailmind/mailmind.toml" }
    }
  }
}
```

`--directory` moves the working directory into the checkout, which is why
`MAILMIND_CONFIG` is absolute here. Give `database_url` an absolute path too, or the
database lands in the checkout and a second way of starting it finds an empty one.

**From the agent's own repository**, which is the tidiest if you are writing one: declare
mailmind as a dev dependency with a source, and let uv keep the two in step.

```toml
[dependency-groups]
dev = ["mailmind[secrets]"]

[tool.uv.sources]
mailmind = { path = "../ronny.email.worker", editable = true }
```

`uv run mailmindctl …` from that repository then runs the checkout — editable, so an edit
over there needs no reinstall, though a *dependency* change needs `uv sync`. A client
spawns it the same way as above with `--directory` pointing at the agent repository.

**No checkout at all**, and the same repository straight from GitHub:

```toml
[tool.uv.sources]
mailmind = { git = "https://github.com/RonnyPfannschmidt/ronny.email.worker", branch = "main" }
```

uv resolves that to a commit and pins it in `uv.lock`, so it is a fixed version until you
ask for another with `uv lock --upgrade-package mailmind`. `uvx --from
git+https://github.com/RonnyPfannschmidt/ronny.email.worker mailmindctl …` is the same
thing without a project around it.

## A checkout when there is one, the ref when there is not

There is no source that resolves to a path if the directory exists and to a git ref
otherwise, and it is worth knowing why before looking for one. `[tool.uv.sources]` does
take a *list* of sources, but the condition on each is a PEP 508 marker — platform, Python
version, implementation — and no marker can ask whether a directory is there. A local
`uv.toml` cannot help either: uv refuses `sources` in one outright, saying it belongs to a
project and so to `pyproject.toml`.

What does work, in decreasing order of tidiness:

**Two groups, declared conflicting.** The pyproject carries both sources, and you pick.

```toml
[dependency-groups]
local = ["mailmind[secrets]"]
remote = ["mailmind[secrets]"]

[[tool.uv.sources.mailmind]]
path = "../ronny.email.worker"
editable = true
group = "local"

[[tool.uv.sources.mailmind]]
git = "https://github.com/RonnyPfannschmidt/ronny.email.worker"
branch = "main"
group = "remote"

[tool.uv]
conflicts = [[{ group = "local" }, { group = "remote" }]]
```

Written as tables rather than one inline table per source, because an inline table has to
fit on a line and that git URL does not leave room.

`uv run --group local …` gets the checkout, `--group remote` the ref. Without the
`conflicts` declaration uv refuses to lock at all, because two URLs for one package is
normally a mistake.

**The git source, overridden when you have the checkout.** `uv run --with-editable
../ronny.email.worker …` shadows whatever the lock says, with nothing committed. A path
that is not there is an error rather than a shrug, and there is no environment variable for
the flag, so the condition has to live outside uv:

```sh
#!/bin/sh
# mailmind-mcp — prefer a checkout, fall back to the pushed ref
checkout=$HOME/src/ronny.email.worker
if [ -d "$checkout" ]; then
    exec uv run --directory "$checkout" mailmindctl mcp "$@"
fi
exec uvx --from git+https://github.com/RonnyPfannschmidt/ronny.email.worker \
    mailmindctl mcp "$@"
```

Point the MCP client's `command` at that script and the question answers itself per
machine, which is where it actually differs.
