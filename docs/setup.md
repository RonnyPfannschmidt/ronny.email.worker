# Setting it up

There is no release yet, so this starts from a checkout.

```
git clone https://github.com/RonnyPfannschmidt/ronny.email.worker
cd ronny.email.worker
uv tool install .          # a mailmindctl on PATH, which MCP clients need
```

`uv sync` and `uv run mailmindctl …` works too, and is what you want if you are also
changing the code. MCP client configurations spawn a bare `mailmindctl`, so for those the
tool install is the easier half.

Add `--with keyring` to the tool install if the password is going in the desktop secret
store.

## Or run a checkout in place

For the latest rather than the last install, three shapes depending on where the agent
lives. All of them run the code in the checkout, so `git pull` is the upgrade.

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

A git source is the same line with `{ git = "…", branch = "…" }` instead of a path, once
there is a branch carrying the package: today `main` holds the design notes and nothing
installable, so uv reports that it does not look like a Python project.

## Point it at a mailbox

```
cp mailmind.toml.example ~/.config/mailmind/mailmind.toml
```

Edit the host and username, and decide where the password lives — the one part of this that
is not mechanical. [Configuration](reference/configuration.md#where-the-password-lives) lays
out the three schemes. The file never holds a password, only a URL saying where one is.

```
mailmindctl bootstrap    # migrate, and write the account into a row
mailmindctl probe        # declared capabilities against what the server offers
mailmindctl sync         # fill the cache
mailmindctl serve        # review UI and MCP endpoint, and a link to open
```

`bootstrap` is idempotent; run it again after adding an account to the file. `probe` exits
non-zero when the server does not offer something the account declares, which is the
direction that would otherwise fail at three in the morning.

## Then

[Connect a client](connecting.md), and keep the mailbox you care about away from it until
you have watched it propose something and rejected it. A
[test drive](test-drive.md) against a container is six commands and touches nothing of
yours.

## What is not there yet

- No packaged release, so no `pip install mailmind`.
- No account form in the review UI: accounts start in the file.
- One tenant, and loopback only. A shared deployment needs something in front doing
  authentication — see [the security model](security-model.md#loopback).
