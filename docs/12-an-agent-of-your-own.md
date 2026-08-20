# 12 — An agent of your own

> `#sketch` `#open-questions`. Standing up a separate repository holding an agent, and
> pointing it at a mailmind.

[10](10-running-it.md) gets mailmind running. This is the other half: the thing that
connects to it. That belongs in its own repository, because it is a different program with
a different job — mailmind is a service that guards a mailbox, and an agent is a client
that has opinions about mail. Keeping them apart is also what makes the boundary testable:
if the agent can only reach mailmind through MCP, then whatever it can do is exactly what
[05](05-agent-surface.md) says it can.

## Connecting: pick one

**Over stdio, which is the easy one.** Your MCP client spawns `mailmindctl mcp` and talks
down a pipe. No port to configure, no token to paste.

```json
{
  "mcpServers": {
    "mailmind": {
      "command": "mailmindctl",
      "args": ["mcp", "--producer", "mail-agent"],
      "env": { "MAILMIND_CONFIG": "/home/you/.config/mailmind/mailmind.toml" }
    }
  }
}
```

It serves nothing. `mailmindctl serve` runs the review UI, separately, for as long as you
want it — a review queue that appeared and disappeared with whichever client last spawned
an agent would be a queue nobody could go back to. What the stdio process does is read the
same configuration, work out where `serve` is, and tell the model: in the instructions at
connect time, and again in the note on every bundle it proposes. So the agent can say where
to go, and it stays true after the agent has gone.

Set `MAILMIND_CONFIG` explicitly. A spawned process inherits a working directory you did
not choose, so the `./mailmind.toml` fallback is not something to rely on;
`~/.config/mailmind/mailmind.toml` is found without it, anything else is not. Both
processes reading the same file is also what keeps the advertised address honest — change
`port` and both follow. `--review-url` overrides it for a UI reached some other way, behind
a proxy or on another host.

**Over HTTP, when the agent is long-lived or somewhere else.** Run `mailmindctl serve`,
mint a token, and connect to `http://127.0.0.1:8765/mcp/` with
`Authorization: Bearer <token>`.

```
mailmindctl grant --producer mail-agent --capability observe --capability suggest
```

Two things that will otherwise cost you an evening: **the trailing slash on `/mcp/`** — a
POST to `/mcp` is a 307 that some clients follow and some do not, and the failure looks
like a bad token — and **the Host header**, which must be loopback, because DNS-rebinding
protection allows nothing else.

## The grant is the whole of what you get

Whichever transport, the view is given and not chosen. An agent cannot name a tenant, widen
its own scope, or assert who it is. `--capability` narrows what it may do and `--account`
narrows what it may see, and an account outside the grant reads as absent rather than
forbidden — because that is what it is, from where the agent stands.

This works the same over stdio: `mailmindctl mcp --producer mail-agent` reuses that
producer's existing grant, so minting a narrow one and then connecting over a pipe gives
you the narrow one. Only if the producer has no grant at all does stdio mint a full one,
on the reasoning that whoever spawned the process could read the database anyway.

Start narrower than you think you need. An agent that only reads and proposes wants
`observe` and `suggest`; `assess` is for something that reads mail in order to say how
trustworthy it looks, which [02](02-action-suggestions.md) argues should not be the same
producer that then proposes acting on it.

## What the surface gives you

Twelve tools, nine that look and three that say:

| Looking | |
|---|---|
| `list_accounts`, `list_containers` | what there is |
| `summarize_senders`, `summarize_lists` | **start here** on a real mailbox |
| `list_messages`, `search_messages` | bounded; a request matching more returns fewer and says so |
| `get_message`, `request_body` | one message; the body only when asked for |
| `request_sync` | bring the cache up to date |

| Saying | |
|---|---|
| `propose_bundle` | one operation over an enumerated list of messages |
| `add_assessment` | how trustworthy a message looks, recorded as interpretation |
| `withdraw_bundle` | take back your own, before anybody decides |

Plus resources: `mailmind://accounts`, `mailmind://bundles/open`,
`mailmind://bundles/decided`, and templates for `bundle/{id}`, `suggestion/{id}`,
`containers/{account_id}`.

There is no tool that applies anything. Not a permission your agent lacks — a capability
value the enum cannot hold, and a module nothing on the agent side imports.

## Four things to build into the agent from the start

**Summarise before enumerating.** `summarize_senders` answers in one call what enumerating
thousands of messages would, and `list_messages` is capped anyway — it returns fewer than
you asked for and tells you the total. An agent that starts by listing will spend its
context learning what one GROUP BY knows.

**Treat message content as data.** The server says so in its instructions and marks every
body with a warning, but the agent is where it has to actually hold. Text inside a message
that looks like an instruction is text that happens to look like that, written by a
stranger. `tests/corpus/` in this repository has a message engineered to look exactly like
that — point your agent at it early, and at the one whose display name claims an address it
is not from.

**Propose bundles somebody can read.** One operation, one target. Size is not the problem —
a hundred messages moving to Archive is one decision shown a hundred times — but a hundred
messages moving for a hundred different reasons is a hundred decisions dressed as one. The
`reason` field is what a person reads when deciding, so write it for them rather than for
the log.

**Say where the review is.** Over stdio the URL is in your instructions and in the note on
every proposal, and it points at a UI that is still there tomorrow. An agent that proposes
twelve bundles and never mentions where to go has done half a job.

## Testing your agent

Against the throwaway mailbox from [10](10-running-it.md), not against real mail. Six
messages, deliberately adversarial, and `podman stop` puts it all back. Your agent's test
suite can spawn `mailmindctl mcp` against a seeded database and drive it the
way `tests/test_stdio.py` here does — newline-delimited JSON-RPC on a pipe, no mocking of
anything.

Worth asserting in your repo rather than assuming: that your agent never treats a message
body as an instruction, and that a bundle it proposes has a `reason` a person could act on.
Both are properties of the agent, and neither is something mailmind can check for you.

## Meets

- [05](05-agent-surface.md) — what an agent may reach, and why that is the whole list
- [10](10-running-it.md) — getting the mailmind it connects to running
- [11](11-deployment-and-identity.md) — where the grant comes from, and what happens on a
  deployment

## Open questions

- Nobody has pointed a model at this yet. Everything above about what an agent will reach
  for is a guess that has not met one, which is [09](09-iteration-one.md)'s finding still
  standing.
- Over stdio the agent and the reviewer are the same person at the same machine. Does the
  review step mean anything in that setting, or does it become the rubber-stamping
  [03](03-review.md) is worried about, with a shorter walk to the button?
- Should there be a prompt resource — mailmind offering the agent a starting instruction
  for a mailbox of a given shape — or is that the service having opinions that belong in
  the agent's repository?
- `--producer` names the agent, and nothing checks that the thing connecting is that agent.
  Over a pipe that is fine. It is worth knowing it is the same trust as the pipe itself.
