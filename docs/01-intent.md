# 01 — Intent

Get real help with mail out of agents, without ever handing them the ability to damage it.

That is the whole of it. Everything else in these documents is a consequence of taking it
seriously.

## Why it is not straightforward

Two things make mail a bad place to point an agent.

**Mail has no undo.** A deleted message is gone, a misfiled one is lost until someone
notices, and a sent reply cannot be recalled. Most software can afford to be wrong
occasionally and fix it after. This cannot.

**Mail is written by strangers.** Anything an agent reads may be trying to steer it. So the
input an agent is most useful on is also the input most likely to be adversarial — and those
are the same messages, not different ones.

Together they mean the failure mode is not "the agent makes a mistake". It is "the agent is
talked into an irreversible action by the thing it was asked to look at".

## What follows from that

Usefulness comes from the agent. Authority stays with the person.

1. An agent is given a view of some mail.
2. It says what should happen — concretely: these messages, this change. That is an
   [action suggestion](02-action-suggestions.md).
3. The person who owns the mail sees what would actually happen, and accepts or rejects it.
4. Only then does the service touch the mailbox.

If the mailbox moved on in between — the person already filed it, another client moved it —
the suggestion is dead rather than applied to something it no longer describes.

The service also watches mailboxes itself and suggests things of its own. Same kind of thing,
same review. An agent's suggestion and the service's own are not treated differently.

## What that costs, and why it is still worth it

Review is unavoidable by construction. Agents get no way to apply anything at all — not a
permission they lack, but a capability absent from their side of the service. That is
deliberate, and it is the expensive part: it means someone reads every suggestion.

It should not stay that way forever. Eventually a person should be able to hand over standing
authority within limits — an agent gets its own folder, or a rule says filing mail from this
sender needs no review. Deliberately not in this version, but the review step is meant to be
where that authority plugs in, so adding it later is not a rebuild.

Starting permissive and tightening later does not work here, because the thing you would be
tightening is the part that already deleted something.

## Not what this is for

- Sending mail. Drafting, yes; sending stays with the person and their own client.
- Replacing a mail client. This is not where mail is read.
- Applying anything automatically, in this version.

## Who it is for

Several people sharing one deployment, each with their own mail accounts — IMAP, or Gmail
through its API.

---

Partly derived from [`design-history/2026-08-12-mailmind-testability-and-ci-plan.md`](design-history/2026-08-12-mailmind-testability-and-ci-plan.md),
which is a starting point, not a decision. That document calls action suggestions "ideas".
