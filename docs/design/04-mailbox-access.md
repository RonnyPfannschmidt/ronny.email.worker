# 04 — Mailbox access

Talking to the actual mail servers. IMAP, and Gmail through its API.

> First cut. Written to be argued with.

## One model, two backends

Folders and labels are both *containers*. A message sits in one or more of them. IMAP mostly
allows one, Gmail allows several — so multi-membership is something a backend either offers or
does not, rather than something the model picks a side on.

The rest of the service should not know which backend it is talking to. What it should know is
what that backend can promise.

## Backends promise different things

Some things one server can do and another cannot: change-detection strong enough to say "only
if this message has not been touched", moving without losing identity, telling you what changed
since last time.

So each account carries a written-down list of what it can do, and the service checks that list
against reality rather than discovering capabilities at runtime and hoping. A capability that
turns out to be missing is a loud failure, not a quiet downgrade.

## Operations

Flag, move, label, delete. Create or edit a draft. Make a container, and remove an empty one.
Nothing else — sending is not here.

Each operation says how strong a guarantee it needs. Some must fail rather than act if the
message changed underneath; some can accept a weaker promise, but then they have to *say* they
did rather than claim a guarantee they did not get.

The two container operations are not a separate kind of authority. A move may name a container
that does not exist, and making it is part of applying that move — so what authorises the
container is the same acceptance that authorises the mail going into it. Removing one is only
offered while it holds nothing, which is what makes it the one removal here that cannot lose
mail; emptiness is a premise like any other, and it is checked again against the server
immediately before the container goes.

What a server does when asked to remove a container that still has containers under it is not
settled — RFC 3501 lets it refuse, and lets it drop the name and leave the children orphaned.
Both have been seen. So this is not something to build on: a bundle removes a branch deepest
first and checks for children itself, rather than letting the answer depend on which server it
is talking to.

## When a connection is unwell

Reading works, writing does not; or nothing works and someone has to look at it. Suggestions
are not applied against an account that is not healthy.

Identifiers can also stop meaning what they meant — an IMAP folder can be recreated, a Gmail
change cursor can expire. When that happens, everything remembered about that container is
suspect and suggestions resting on it are dead.

## Meets

- [02 — Action suggestions](02-action-suggestions.md) — decides whether an action is even
  expressible against a given account
- 06 — The core — resyncs, and marks suggestions dead when identity breaks
- 08 — Untrusted content — everything read here is hostile input

## Open questions

- Does the container abstraction survive contact with Gmail, or does pretending labels are
  folders cost more than handling them separately?
- How much is cached locally, and is a local copy of mail state a liability worth having?
- Is Gmail-over-IMAP a third backend or a mistake?
- What does the service do when a capability check fails at three in the morning — stop, or
  keep reading and refuse to write?
- Does draft editing need a stronger guarantee than the others? A person editing the same draft
  in their own client is a race with a nastier outcome than a misfiled message.
