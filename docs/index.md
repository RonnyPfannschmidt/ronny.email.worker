# mailmind

Get real help with mail out of agents, without ever handing them the ability to damage it.

An agent connects over MCP, browses the mail its grant covers, and says what should happen
to it. It cannot make any of it happen. A person reviews the proposed effect — every
message, where it is, where it would go — and accepts or rejects. Only then does the
service touch the mailbox, and only if nothing has moved in the meantime.

## What that means concretely

There is no tool on the agent surface that applies a change. Not a permission an agent
lacks: `apply` is not a value the capability enum can hold, and the module that writes to
a mailbox is not imported by anything on the agent side. The single path from a proposal
to a mailbox runs through the review UI, and that UI has a login the agent is never given.

The promise is worth stating exactly, because a looser version of it would be false:

> Nothing reaches a mailbox through mailmind except an accept made by something holding
> the review key, and every accept is recorded against a producer.

Not "a person accepted it". [The security model](security-model.md) is where that
distinction is spelled out, along with what it costs you if your agent has a shell.

## Where to go

| If you want to | Read |
|---|---|
| See it work, against a mailbox that is not yours | [Getting started](getting-started.md) |
| Point it at your own mailbox | [Configuration](configuration.md) |
| Decide whether to trust it | [Security model](security-model.md) |
| Work through a queue | [Reviewing](reviewing.md) |
| Connect an agent of your own | [Connecting an agent](agents.md) |
| Look something up | [`mailmindctl`](reference/cli.md), [the MCP surface](reference/mcp.md) |
| Know why any of it is shaped this way | [Design notes](design/index.md) |

## Status

The first iteration is built: IMAP, one tenant, and enough to sort a long untended
mailbox. It has been driven by scripts and by a person, and — as
[09](design/09-iteration-one.md) keeps saying — not yet by a model. Anything in these
pages about what an agent will reach for is a guess that has not met one.
