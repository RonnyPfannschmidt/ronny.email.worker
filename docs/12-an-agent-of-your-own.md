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

Either way the model is told where the review UI is — in the instructions at connect time
and in the note on every bundle — so the agent can tell whoever it is working for where to
go. What `--serve` decides is whether that UI is this process or another one.

**Without it** the address comes from the configuration, and is expected to be a
`mailmindctl serve` that outlives any one session. That is the shape to want when the queue
is something you come back to: an agent proposes twenty bundles at nine in the morning and
you work through them at four, long after the client that spawned it has gone.

**With it** this process brings the UI up itself and takes it down again at the end:

```json
"args": ["mcp", "--producer", "mail-agent", "--serve", "--port", "0"]
```

That is the whole of the setup for somebody whose agent is the only thing that ever
proposes anything. Start the agent, get told where to review, review it while the agent is
still there. `--port 0` takes a free one, so several clients can each have their own
without colliding; without it the configured port is used, and if a `serve` already has it
you get told to drop the flag rather than a stack trace. Nothing else is mounted on it —
the agent is already on the pipe, so `/mcp/` is not there.

Set `MAILMIND_CONFIG` explicitly. A spawned process inherits a working directory you did
not choose, so the `./mailmind.toml` fallback is not something to rely on;
`~/.config/mailmind/mailmind.toml` is found without it, anything else is not. Both
processes reading the same file is also what keeps the advertised address honest — change
`port` and both follow. `--review-url` says where a UI already is, for one reached some
other way, behind a proxy or on another host.

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

## The port the agent is told about and must not use

The review UI is served for the person at this computer. The agent is told the address so
it can pass it on, and the agent is not to use it. That sentence is the design; the rest of
this section is about the distance between it and what is actually enforced.

### Why it carries so much

In the `--serve` shape it is the whole boundary. The agent surface has no apply — not a
permission withheld but a capability value the enum cannot hold — and the session's UI
mounts no MCP endpoint, so there is no second way in through the port either. Which means
the only path from a proposal to a mailbox runs through the review UI, and the only thing
between the agent and that path is that the agent is not supposed to take it.

### What used to be enforced, and what is now

Nothing, at first. The review UI has no login, deliberately
([11](11-deployment-and-identity.md)), and accepting was an ordinary form POST. Holding the
URL the model had just been handed:

```
POST http://127.0.0.1:35607/bundle/1/accept  ->  200
  INBOX: 5 (was 6)    Archive: 1 (was 0)
```

No token, no browser, no form fields. One request, and mail moved.

Every route that changes something now requires the headers a browser sends when a person
submits a form on a page it is showing:

```
sec-fetch-mode: navigate      sec-fetch-site: same-origin
sec-fetch-dest: document      origin:          <this origin>
```

These are [Fetch Metadata](https://developer.mozilla.org/en-US/docs/Glossary/Fetch_metadata_request_header)
headers, which scripts are forbidden to set — inside a browser they cannot be forged. The
same request as above now gets a 403 that says why, and leaves a `ui_change_refused` event
behind, because a refusal is the interesting half: nothing changed, but something tried,
and whoever owns the mail should be able to find that out afterwards.

`Sec-Fetch-User: ?1` would be the better signal — "a person did this" rather than "a
document navigated" — and is not required, because Safari has never sent it and a check
that locks out a whole browser is a check somebody turns off.

### What this is and is not

It is not a security boundary and cannot be made into one. Anything that can set a header
can assert all four, and doing so takes about a line:

```
POST with the four headers  ->  200, and the mail moves
```

What changed is the character of the act. Before, an agent reached the review UI by doing
the obvious thing with an address it had been given. Now it reaches it by stating, in
headers it had to choose to send, that a browser is showing a page to a person. The first
is an accident waiting to happen; the second is a lie. That is the whole of what a check
like this buys, and it is worth having for exactly that reason and no other.

The things that would make it a real boundary have not changed:

- **The address was given away on purpose.** That is the feature: the agent knows where the
  review is so it can tell whoever it is working for. The same fact makes the port
  reachable.
- **`--port 0` looks like entropy and is not.** It is handed over in the instructions, so
  the unpredictability is spent the moment the session starts.
- **A form token would raise the cost again** — you would have to fetch and parse the page
  you intend to act on, which is at least thematically right for a review UI. It is still
  only cost.
- **HTTP cannot express "a person did this".** That is the actual requirement, and no
  request can carry it.

### What would make it a boundary

Roughly in increasing order of how much they actually hold:

1. **Do not tell the agent the address.** Print it to stderr only, and tell the model that a
   review UI is running and where its operator can find the address. `--port 0` then becomes
   a real capability rather than a formality. Cheap — and it costs exactly the thing that
   made this mode nice, which is the agent being able to say where to go. Note that the
   gesture check above does not help here and was never meant to: it raises the cost of
   using an address, not of holding one.
2. **A secret in the path, given to the person and not to the model.** The agent can say
   "open the link printed in your terminal"; only the human holds a URL that works. Keeps
   most of the ergonomics and makes the capability explicit rather than incidental.
3. **Take the agent off loopback.** A network namespace, a container, anything without a
   route to the host's own interface. This is the only one that holds regardless of what the
   agent knows or is told, and it is a property of how the agent is run rather than anything
   mailmind can do.
4. **Put the reviewer where the agent has no path at all** — another machine, or a review
   surface that is not HTTP: a TUI on the person's terminal, a desktop notification. The
   strongest and the largest change.

Note that none of this is special to `--serve`. A separately-run `mailmindctl serve` on
`127.0.0.1:8765` is reachable by the same agent and is easier to guess, not harder. What
`--serve` changes is that the address is handed over deliberately, which makes visible
something that was already true.

### What it means for the review step

[03](03-review.md) asks what stops review becoming rubber-stamping. On one machine the
question has a sharper form: what stops it being self-approval. If the agent can accept its
own proposals, then in that configuration the review step is decorative, and the design's
central claim — that a person decides — is true only because nothing has tried otherwise.

The claim is worth stating precisely, because it is defensible in a narrower form: mailmind
guarantees that **nothing reaches a mailbox except through an accept**, and that every
accept is recorded against a producer. It does not currently guarantee that the accept came
from a person. Those are different promises, and only the first one is kept by the code.

## Meets

- [05](05-agent-surface.md) — what an agent may reach, and why that is the whole list
- [10](10-running-it.md) — getting the mailmind it connects to running
- [11](11-deployment-and-identity.md) — where the grant comes from, and what happens on a
  deployment

## Open questions

- Nobody has pointed a model at this yet. Everything above about what an agent will reach
  for is a guess that has not met one, which is [09](09-iteration-one.md)'s finding still
  standing.
- Which of the four mitigations above is worth building, and does the first one cost more
  than it buys? An agent that cannot name the review address has to say "check your
  terminal", which is worse to use and honest.
- Is "the accept came from a person" a promise mailmind should try to make at all, or one it
  should state plainly that it does not make and leave to how the agent is run?
- Should there be a prompt resource — mailmind offering the agent a starting instruction
  for a mailbox of a given shape — or is that the service having opinions that belong in
  the agent's repository?
- `--producer` names the agent, and nothing checks that the thing connecting is that agent.
  Over a pipe that is fine. It is worth knowing it is the same trust as the pipe itself.
