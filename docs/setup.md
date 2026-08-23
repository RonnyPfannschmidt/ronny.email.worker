# Setting it up

There is no release yet, so this starts from a checkout.

```
git clone https://github.com/RonnyPfannschmidt/ronny.email.worker
cd ronny.email.worker
uv tool install .          # a mailmindctl on PATH, which MCP clients need
```

`uv sync` and `uv run mailmindctl …` works too, and [running a
checkout](running-a-checkout.md) has the shapes for tracking the code rather than a copy of
it. MCP client configurations spawn a bare `mailmindctl`, so for those the
tool install is the easier half.

Add `--with keyring` to the tool install if the password is going in the desktop secret
store.

## Point it at a mailbox

```
cp mailmind.toml.example ~/.config/mailmind/mailmind.toml
```

Edit the host and username, and decide where the password lives — the one part of this that
is not mechanical. [Configuration](reference/configuration.md#where-the-password-lives) lays
out the four schemes. The file never holds a password, only a URL saying where one is.

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
