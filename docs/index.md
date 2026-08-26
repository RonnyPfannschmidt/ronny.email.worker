# mailmind

An MCP server for somebody's mail, with a review UI beside it.

An agent connects, browses the mail its grant covers, and says what should happen to it.
It cannot make any of it happen. A person reviews the proposed effect — every message,
where it is, where it would go — and accepts or rejects. Only then does the service touch
the mailbox, and only if nothing has moved since.

> Nothing reaches a mailbox through mailmind except an accept made by something holding
> the review key, and every accept is recorded against a producer.

Not "a person accepted it". [The security model](security-model.md) has the difference.

## Where to go

| | |
|---|---|
| [Connecting a client](connecting.md) | Both transports, grants, what an agent gets |
| [Reviewing](reviewing.md) | Where a proposal becomes a change |
| [Security model](security-model.md) | What it promises, and where that stops |
| [Test drive](test-drive.md) | Six commands, a container, nothing of yours |
| [Setting it up](setup.md) | Your own mailbox |
| [Running a checkout](running-a-checkout.md) | The dev version, from wherever the agent lives |
| [Reference](reference/mcp.md) | The MCP surface, `mailmindctl`, the configuration file |
| [Design notes](design/index.md) | Why it is shaped this way |

Iteration one: IMAP, one tenant, no release — you run it from a checkout. No model has
been pointed at it yet ([09](design/09-iteration-one.md) has what that leaves open).
