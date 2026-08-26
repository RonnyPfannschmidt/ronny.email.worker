# 12 — An agent of your own

> `#sketch` `#open-questions`. Standing up a separate repository holding an agent, and
> pointing it at a mailmind.

[10](10-running-it.md) gets mailmind running. This is the other half: the thing that
connects to it. That belongs in its own repository, because it is a different program with
a different job — mailmind is a service that guards a mailbox, and an agent is a client
that has opinions about mail. Keeping them apart is also what makes the boundary testable:
if the agent can only reach mailmind through MCP, then whatever it can do is exactly what
[05](05-agent-surface.md) says it can.

## Connecting

Two transports, and the choice between them is about how long the agent lives rather than
about what it may do. Over stdio a client spawns `mailmindctl mcp` and talks down a pipe —
no port, no token. Over HTTP a long-lived or remote agent presents a bearer token to
`/mcp/`. The configurations for both are in
[Connecting an agent](../connecting.md), and shipped ready to copy in `integrations/`.

What `--serve` decides is not whether the model is told where the review UI is — it always
is, at connect time and on every bundle — but whether that UI is this process or another
one. Without it the address comes from the configuration and is expected to be a
`mailmindctl serve` that outlives any one session: the shape to want when an agent proposes
twenty bundles at nine in the morning and you work through them at four. With it, the
session brings its own UI up and takes it down again, which is the whole of the setup for
somebody whose agent is the only thing that ever proposes anything.

Both processes reading the same configuration is what keeps the advertised address honest —
change `port` and both follow.

## The grant is the whole of what you get

Whichever transport, the view is given, not chosen ([05](05-agent-surface.md)) —
an agent cannot name a tenant, widen
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

Fifteen tools, ten that look and five that say, plus six resources and three prompts;
[the reference](../reference/mcp.md) is the list, and [Connecting an agent](../connecting.md)
has what to build into an agent from the start.

There is no tool that applies anything —
[the security model](../security-model.md) has what makes that structural.

The prompts are this iteration's guess at [05](05-agent-surface.md)'s question — what an
agent needs in order to be useful here — offered rather than imposed, because a client that
never calls `prompts/get` must get the same tools and the same refusals. Each one repeats
the same ground rules, because a client picks one prompt and never sees the others. It is a
guess that has still not met a model.

Test an agent against the throwaway mailbox rather than against real mail. `tests/corpus/`
here has a message engineered to look exactly like an instruction, and one whose display
name claims an address it is not from; both are worth pointing an agent at early. Whether
it treats a body as instruction, and whether the `reason` on a bundle is one a person could
act on, are properties of the agent — neither is something mailmind can check for you.

## The port the agent is told about and cannot open

The review UI is served for the person at this computer. The agent is told the address so
it can pass it on, and is not given the key that opens it. That asymmetry is the design;
how it was arrived at — three attempts, two of which were not enough — is
[history](history/2026-08-26-how-the-review-ui-got-a-login.md).

### Why it carries so much

In the `--serve` shape it is the whole boundary. The agent surface has no apply
([the security model](../security-model.md) has what holds that up), and the session's UI
mounts no MCP endpoint, so there is no second way in through the port either. The only
path from a proposal to a mailbox runs through the review UI.

### What it holds today

A key is minted when the process starts and left where the person is: `serve` prints the
link outright, because a terminal is nobody's agent log; `mcp --serve` writes it to a
file only you can read and gives stderr the path, because an MCP client's log can end up
in front of the model. Following the link once trades the key for an HttpOnly session
cookie and drops it out of the address bar; `mailmindctl review --open` follows it for
you. The model is told the address and nothing else, and the test suite asserts the key
is absent from every channel that reaches an agent.

### What it actually buys, and where it stops

It moves the bar from *any agent that can make an HTTP request* to *an agent that can read
the person's terminal or files*. That is a real step and it is the last one this process
can take by itself.

It stops there completely. An agent with a shell on the same account does not need the
review UI at all: it can read `mailmind.toml`, resolve the mailbox password through the
very indirection that keeps it out of the config file, and talk IMAP itself. At that point
mailmind is not in the path and nothing it does matters. **The boundary that holds is how
the agent is run** — a sandbox, a container, an account of its own — and that is not
something this can draw for you.

So the promise is worth stating exactly:

> Nothing reaches a mailbox through mailmind except an accept made by something holding
> the review key, and every accept is recorded against a producer.

Not "a person accepted it". If the thing holding the key is a person, mailmind's boundary
is the design's boundary. If your agent can read your terminal, it is not, and no amount
of checking inside this process changes that.

## Meets

- [05](05-agent-surface.md) — what an agent may reach, and why that is the whole list
- [10](10-running-it.md) — getting the mailmind it connects to running
- [11](11-deployment-and-identity.md) — where the grant comes from, and what happens on a
  deployment

## Open questions

- Nobody has pointed a model at this yet. Everything above about what an agent will reach
  for is a guess that has not met one, which is [09](09-iteration-one.md)'s finding still
  standing.
- The link file sits in `$XDG_RUNTIME_DIR`, readable by this user and so by anything
  running as them. That is the same boundary as everything else here, which is the point,
  but a desktop notification or the controlling terminal would be narrower.
- A restart mints a new key, so an open tab stops working and you go back to the terminal.
  Right, or should the key persist in the state directory so a restart is invisible?
- Nothing binds a session to the browser that opened it. Somebody who gets the cookie has
  it until the browser closes. For a local single-user tool that is probably proportionate.
- Is "the accept came from a person" worth trying to promise at all, given that the honest
  boundary is how the agent is sandboxed?
- Should there be a prompt resource — mailmind offering the agent a starting instruction
  for a mailbox of a given shape — or is that the service having opinions that belong in
  the agent's repository?
- `--producer` names the agent, and nothing checks that the thing connecting is that agent.
  Over a pipe that is fine. It is worth knowing it is the same trust as the pipe itself.
