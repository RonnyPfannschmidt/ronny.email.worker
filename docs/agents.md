# Connecting an agent

The agent belongs in its own repository. mailmind is a service that guards a mailbox; an
agent is a client with opinions about mail. Keeping them apart is also what makes the
boundary testable: if the agent can only reach mailmind through MCP, then what it can do is
exactly what [the surface](reference/mcp.md) says it can.

## Over stdio, which is the easy one

Your MCP client spawns `mailmindctl mcp` and talks down a pipe. No port, no token.

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

Ready-made files for this shape and for opencode's schema are in `integrations/` in the
repository, along with where each one goes.

**Set `MAILMIND_CONFIG` explicitly.** A spawned process inherits a working directory you
did not choose, so the `./mailmind.toml` fallback is not something to rely on.

**`--serve --port 0`** brings the review UI up for the life of the session, on a free port
so several clients do not collide. That is the whole of the setup for somebody whose agent
is the only thing that ever proposes anything: start the agent, get told where to review,
review it while the agent is still there. Nothing else is mounted on that port — the agent
is already on the pipe, so `/mcp/` is not there.

**Without `--serve`**, the address comes from the configuration and is expected to be a
`mailmindctl serve` that outlives any one session. That is the shape to want when the queue
is something you come back to: an agent proposes twenty bundles at nine in the morning and
you work through them at four. `--review-url` names a UI reached some other way — behind a
proxy, or on another host.

Either way the model is told where the review UI is, at connect time and again on every
bundle. It is never told the key that opens it.

## Over HTTP, when the agent is long-lived or elsewhere

```
mailmindctl serve
mailmindctl grant --producer mail-agent --capability observe --capability suggest
```

Connect to `http://127.0.0.1:8765/mcp/` with `Authorization: Bearer <token>`. The token is
printed once; only its hash is stored.

Two things that will otherwise cost you an evening: **the trailing slash on `/mcp/`** — a
POST to `/mcp` is a 307 that some clients follow and some do not, and the failure looks
like a bad token — and **the Host header**, which must be loopback, because DNS-rebinding
protection allows nothing else.

## Ask for less than you think you need

`--capability` narrows what the agent may do, `--account` narrows what it may see. An agent
that reads and proposes wants `observe` and `suggest`. See
[the security model](security-model.md#what-a-grant-is) for what each one covers and how a
stdio connection reuses a grant minted this way.

## Four things to build in from the start

**Summarise before enumerating.** `summarize_senders` answers in one call what enumerating
thousands of messages would, and `list_messages` is capped anyway — it returns fewer than
you asked for and tells you the total. An agent that starts by listing will spend its
context learning what one GROUP BY knows.

**Treat message content as data.** The server says so in its instructions and marks every
body with a warning, but the agent is where it has to hold. Text inside a message that
looks like an instruction is text that happens to look like that, written by a stranger.

**Propose bundles somebody can read.** One operation, one target. The `reason` field is
what a person reads when deciding, so write it for them rather than for the log.

**Say where the review is.** The URL is in your instructions and in the note on every
proposal. An agent that proposes twelve bundles and never mentions where to go has done
half a job.

## Prompts, which carry the guardrails

Three, offered rather than imposed — a client that never calls `prompts/get` gets the same
tools and the same refusals:

| Prompt | For |
|---|---|
| `triage_mailbox` | working through a long folder, in the order that survives a real mailbox |
| `assess_message` | reading one message carefully without proposing anything |
| `hand_over` | telling the person what is waiting and where to decide on it |

Each repeats the same ground rules, because a client picks one prompt and never sees the
others. If you write your own, the four rules above are the load-bearing part of them.

## Testing your agent

Against the throwaway mailbox from [Getting started](getting-started.md), not against real
mail. Six messages, deliberately adversarial, and `podman stop` puts it all back. Your test
suite can spawn `mailmindctl mcp` against a seeded database and drive it the way
`tests/test_stdio.py` does here — newline-delimited JSON-RPC on a pipe, mocking nothing.

Worth asserting in your repository rather than assuming: that your agent never treats a
message body as an instruction, and that a bundle it proposes has a `reason` a person could
act on. Both are properties of the agent, and neither is something mailmind can check for
you.
