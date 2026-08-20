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
[Connecting an agent](../agents.md), and shipped ready to copy in `integrations/`.

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

Twelve tools, nine that look and three that say, plus six resources and three prompts;
[the reference](../reference/mcp.md) is the list, and [Connecting an agent](../agents.md)
has what to build into an agent from the start.

There is no tool that applies anything. Not a permission your agent lacks — a capability
value the enum cannot hold, and a module nothing on the agent side imports.

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
it can pass it on, and is not given the key that opens it. That asymmetry is the design,
and this section is the history of getting there, because two of the three attempts were
not enough and it is worth knowing why.

### Why it carries so much

In the `--serve` shape it is the whole boundary. The agent surface has no apply — not a
permission withheld but a capability value the enum cannot hold — and the session's UI
mounts no MCP endpoint, so there is no second way in through the port either. The only
path from a proposal to a mailbox runs through the review UI.

### First: nothing at all

The review UI had no login. Accepting was an ordinary form POST, and holding the URL the
model had just been handed was enough:

```
POST http://127.0.0.1:35607/bundle/1/accept  ->  200
  INBOX: 5 (was 6)    Archive: 1 (was 0)
```

No token, no browser, no form fields. One request, and mail moved.

### Second: it has to look like a person did it

Every route that changes something requires the headers a browser sends when somebody
submits a form on a page it is showing — `sec-fetch-mode: navigate`, `sec-fetch-dest:
document`, `sec-fetch-site: same-origin`, and a matching `origin`. These are
[Fetch Metadata](https://developer.mozilla.org/en-US/docs/Glossary/Fetch_metadata_request_header)
headers, which scripts are forbidden to set, so inside a browser they cannot be forged.
A refusal writes a `ui_change_refused` event, because nothing changed but something tried,
and whoever owns the mail should be able to find that out.

`Sec-Fetch-User: ?1` would be the better signal — "a person did this" rather than "a
document navigated" — and is deliberately not required, because Safari has never sent it
and a check that locks out a whole browser is a check somebody turns off.

This was never a boundary and was not meant as one. It changed the character of the act:
before, an agent reached the review UI by doing the obvious thing with an address it had
been given; after, by stating in four headers that a browser was showing a page to a
person. An accident became a lie. But a lie is one line of code:

```
POST with the four headers  ->  200, and the mail moves
```

### Third: a login, for local too

So there is one. Not a password — there is nobody here to have an account, and a
passphrase for a service on your own machine is friction protecting the wrong thing.
Instead a **key**, minted when the process starts and printed where the person is:

```
review UI  http://127.0.0.1:45911/  (this session only)
           the link that opens it is in /run/user/1000/mailmind/review-45911.link
           `mailmindctl review --open` follows it for you
```

Note what is *not* there. An MCP client collects the stderr of everything it spawns into a
log, and some put that log in front of the model — so the key goes to a file with a mode
on it, and stderr gets the path. `mailmindctl serve` prints the link outright, because that
is a command a person runs in their own terminal and its output is nobody's agent log.

Following the link once trades the key for an HttpOnly session cookie and drops it back out
of the address. The model is told `http://127.0.0.1:45911/` and nothing else. The same forged
request as above now gets a 401 that says, in as many words, that an agent reading it was
not given the key on purpose and should tell the person to look at their terminal.

The key is not hard to guess and does not need to be. Everything rests on it not being
told to the agent, which is a property of every channel that reaches one — the
instructions, tool results, resources, prompts, and the stderr an MCP client collects. The
test suite asserts all five.

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
