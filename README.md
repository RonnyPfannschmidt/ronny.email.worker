# ronny.email.worker

`mailmind` — an MCP server and webapp that lets agents work with someone's mailboxes,
without letting them change anything a person has not agreed to.

An agent connects over MCP, browses the mail its grant covers, and says what should happen
to it. It cannot make any of it happen: there is no tool that applies a change, no `apply`
value in the capability enum, and nothing on the agent side imports the code that writes to
a mailbox. A person reviews the proposed effect — every message, where it is, where it
would go — and accepts or rejects. Only then does the service touch the mailbox, and only
if nothing has moved in the meantime.

The first iteration is built: IMAP, one tenant, and enough to sort a long untended mailbox.
See [09 — Iteration one](docs/09-iteration-one.md) for what it does and how to run it.

Start with [the intent](docs/01-intent.md); the whole design is in [docs/](docs/).

```
uv venv && uv pip install -e '.[test]'
cp mailmind.toml.example mailmind.toml     # then edit it
mailmindctl bootstrap && mailmindctl probe && mailmindctl sync
mailmindctl grant --producer opencode      # prints a bearer token, once
mailmindctl serve                          # review UI on /, MCP on /mcp
```
