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
[Running a checkout](running-a-checkout.md) has that shape and the two paths it makes
absolute.

- **Set `MAILMIND_CONFIG`.** A spawned process inherits a working directory you did not
  choose, so the `./mailmind.toml` fallback is not one to rely on.
- **`--serve --port 0`** brings the review UI up for the life of the session, on a free
  port. Without it, the address comes from the configuration and is expected to be a
  `mailmindctl serve` that outlives the session — the shape to want when the queue is
  something you come back to. `--review-url` names a UI reached some other way.

## Over HTTP

```
mailmindctl serve
```

Then point the client at `http://127.0.0.1:8765/mcp/` and let it log in. A client that
speaks MCP's OAuth — `opencode mcp auth mailmind`, and most others — discovers the rest from
the `401`: it registers itself, opens a browser, and you get a page asking what it may do.
Nothing is copied anywhere.

The page is part of the review UI, so it is behind the same login: follow the link
`mailmindctl serve` printed, then agree. What the agent gets is what you tick — it does not
ask for capabilities and cannot ask for too many — and you take it back on the
review UI's `/agents` page.

Behind a proxy, set `public_url` to the address people actually reach, because that is what
the client is told to come back to. See [configuration](reference/configuration.md).

### By hand, for a client that cannot log in

```
mailmindctl grant --producer mail-agent --capability observe --capability suggest
```

Then the same URL with `Authorization: Bearer <token>`. The token is printed once; only its
hash is stored. This still works and is not going away — it is how you drive the endpoint
from `curl`, and the only way in for a client with no OAuth of its own.

Two things that otherwise cost an evening: **the trailing slash** — a POST to `/mcp` is a
307 that some clients follow and some do not, and the failure looks like a bad token — and
**the `Host` header**, which must be loopback, because DNS-rebinding protection allows
nothing else.

## What the connection gets

The view is given, not chosen ([05](design/05-agent-surface.md)): an agent cannot name a
tenant, widen its scope, or assert
who it is. `--capability` narrows what it may do, `--account` what it may see, and an
account outside the grant reads as *absent* rather than forbidden.

| | |
|---|---|
| `observe` | read what is cached |
| `suggest` | propose a bundle, withdraw its own |
| `assess` | record how trustworthy something looks |

There is no fourth. Ask for less than you think you need; `assess` belongs to something
other than the producer that proposes acting on what it read.

Logging in, the choice is not the agent's at all: it asks for nothing in particular and a
person ticks the boxes. `--capability` is the same decision made on the command line.

`message_id` is mailmind's own, and is the only id on this surface: it survives syncs,
moves and expunges, and an agent never sees an IMAP UID. Which id is which, and what moves
them, is [Identifiers](reference/identifiers.md).

Over stdio, `--producer NAME` reuses that producer's grant if it has one, so minting a
narrow one first gives you the narrow one. A producer with no grant at all gets a full one,
on the reasoning that whoever spawned the process could read the database anyway.

The whole surface is in [the reference](reference/mcp.md), rendered from the server.

## Building the agent

Whatever the client, four things carry:

- **Search is words, not syntax, and shallow until bodies arrive.** An address, a domain
  or a URL searches for itself rather than failing on punctuation, and every word has to
  appear. What it looks at is subjects, senders and previews — and a preview exists only
  once something has fetched that message's body, so a folder nobody has opened is
  searchable by who wrote it and what it is called, not yet by what it says.
- **Summarise before enumerating.** `summarize_senders` answers in one call what listing
  thousands of messages would. Every observation is capped by `max_messages_per_request`
  and comes back in one envelope — `returned`, `total_matching`, `truncated`, `note` — so
  a short answer is always distinguishable from a complete one.
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
