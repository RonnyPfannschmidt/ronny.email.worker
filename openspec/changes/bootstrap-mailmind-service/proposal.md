## Why

Letting an agent work on a mailbox is risky in two directions at once. Mail has no undo, so
a wrong action is a real loss. And mail is text written by strangers, so anything an agent
reads may be trying to steer it.

`mailmind` is an attempt to get the usefulness anyway, by never letting an agent's intent
reach a mailbox directly. An agent can look and suggest. A person decides. The service does
the actual work.

## What Changes

This change writes down the rough shape of the service. It does not specify components yet.

**In outline.** Several people share one deployment. Each has mail accounts — IMAP, or Gmail
through its API. Agents connect to the service and are given a view of some of that mail.
When an agent thinks something should happen — this belongs in that folder, this thread is
done, these can go — it says so, concretely: exactly which messages, exactly what change.
That is an *action suggestion*.

Action suggestions queue up for the person who owns the mail. They see what would actually
happen, not a description of it, and accept or reject each one. Only then does the service
touch the mailbox. If the mailbox changed in the meantime — the person already filed it,
another client moved it — the suggestion is dead rather than applied to something it no
longer describes.

The service also watches mailboxes itself and can produce action suggestions of its own.
Those are the same kind of thing and go through the same review; an agent's suggestion and
the service's own suggestion are not treated differently.

**Why it is shaped this way.** The review step is the whole point, so it has to be
unavoidable. Agents get no way to apply anything at all — not a permission they lack, but a
capability that is absent from their side of the service. Applying lives somewhere agents
cannot reach.

**Where it goes later.** Reviewing everything by hand does not scale, and it should not have
to. The intent is that a person can later hand over standing authority within limits — an
agent gets its own folder, or a rule says filing mail from this sender needs no review. That
is deliberately not in this version, but the review step is meant to be the place that
authority plugs into, so adding it later does not mean rebuilding the flow.

**Not in scope.** Sending mail. Automatic application of anything. Deciding the protocols,
storage, or process layout — those come with the components.

## Capabilities

### New Capabilities

None yet. This change establishes the intent; components and their specs are introduced one
at a time in later changes, roughly in this order:

- the **action suggestion** and its lifecycle — the unit everything else is built around
- the **review step** — where a person decides, and what they have to be shown
- **mailbox access** — accounts, IMAP and Gmail, and what each backend can and cannot promise
- the **agent surface** — what an agent may see and say, and why it cannot apply
- the **core** — how suggestions, review, and mailbox state are kept consistent with each
  other
- **tenancy** — keeping several people's mail apart
- **untrusted content** — treating mail as hostile input

### Modified Capabilities

None. `openspec/specs/` is empty.

## Impact

Nothing is built by this change. It fixes the intent that later changes are measured
against.

Partly derived from
[`docs/design-history/2026-08-12-mailmind-testability-and-ci-plan.md`](../../../docs/design-history/2026-08-12-mailmind-testability-and-ci-plan.md),
which is a starting point, not a decision. That document calls action suggestions "ideas".
