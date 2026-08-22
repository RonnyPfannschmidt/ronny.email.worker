# Connecting a client

Two transports, carrying the same surface. Which one you want is about how long the agent
lives, not about what it may do.

## Over stdio

The client spawns `mailmindctl mcp` and talks down a pipe. No port, no token.

```json
{
  "mcpServers": {
    "mailmind": {
      "command": "mailmindctl",
      "args": ["mcp", "--producer", "mail-agent", "--serve", "--port", "0"],
      "env": { "MAILMIND_CONFIG": "/home/you/.config/mailmind/mailmind.toml" }
    }
  }
}
```

`integrations/` in the repository has this and opencode's schema, ready to copy, with where
each file goes. Running mailmind from a git checkout rather than an install changes the
`command` to `uv` and the args to `run --directory …` —
[Setting it up](setup.md#or-run-a-checkout-in-place) has that shape and the two paths it
makes absolute.

- **Set `MAILMIND_CONFIG`.** A spawned process inherits a working directory you did not
  choose, so the `./mailmind.toml` fallback is not one to rely on.
- **`--serve --port 0`** brings the review UI up for the life of the session, on a free
  port. Without it, the address comes from the configuration and is expected to be a
  `mailmindctl serve` that outlives the session — the shape to want when the queue is
  something you come back to. `--review-url` names a UI reached some other way.

## Over HTTP

```
mailmindctl serve
mailmindctl grant --producer mail-agent --capability observe --capability suggest
```

Then `http://127.0.0.1:8765/mcp/` with `Authorization: Bearer <token>`. The token is printed
once; only its hash is stored.

Two things that otherwise cost an evening: **the trailing slash** — a POST to `/mcp` is a
307 that some clients follow and some do not, and the failure looks like a bad token — and
**the `Host` header**, which must be loopback, because DNS-rebinding protection allows
nothing else.

## What the connection gets

The view is given, not chosen: an agent cannot name a tenant, widen its scope, or assert
who it is. `--capability` narrows what it may do, `--account` what it may see, and an
account outside the grant reads as *absent* rather than forbidden.

| | |
|---|---|
| `observe` | read what is cached |
| `suggest` | propose a bundle, withdraw its own |
| `assess` | record how trustworthy something looks |

There is no fourth. Ask for less than you think you need; `assess` belongs to something
other than the producer that proposes acting on what it read.

Over stdio, `--producer NAME` reuses that producer's grant if it has one, so minting a
narrow one first gives you the narrow one. A producer with no grant at all gets a full one,
on the reasoning that whoever spawned the process could read the database anyway.

The whole surface is in [the reference](reference/mcp.md), rendered from the server.

## Building the agent

Whatever the client, four things carry:

- **Summarise before enumerating.** `summarize_senders` answers in one call what listing
  thousands of messages would, and `list_messages` is capped anyway.
- **Treat message content as data.** The server says so and marks every body, but the agent
  is where it has to hold. `tests/corpus/` has a message engineered to look like an
  instruction, and one whose display name claims an address it is not from.
- **Propose bundles somebody can read.** One operation, one target. `reason` is what a
  person reads when deciding.
- **Say where the review is.** The URL is in the instructions and on every proposal. An
  agent that proposes twelve bundles and never mentions where to go has done half a job.

Three prompts — `triage_mailbox`, `assess_message`, `hand_over` — carry those rules for
clients that ask for them. They are offered, not imposed: a client that never calls
`prompts/get` gets the same tools and the same refusals.

Test against [the throwaway mailbox](test-drive.md), driving `mailmindctl mcp` on a pipe the
way `tests/test_stdio.py` does here.
