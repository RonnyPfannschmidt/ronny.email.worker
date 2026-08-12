# 01 — The idea

Letting an agent work on a mailbox is risky in two directions at once. Mail has no undo, so
a wrong action is a real loss. And mail is text written by strangers, so anything an agent
reads may be trying to steer it.

`mailmind` tries to get the usefulness anyway, by never letting an agent's intent reach a
mailbox directly.

> An agent looks and suggests. A person decides. The service does the work.

## The loop

1. An agent is given a view of some mail.
2. It says what should happen — concretely: these messages, this change. That is an
   [action suggestion](02-action-suggestions.md).
3. The person who owns the mail sees what would actually happen, and accepts or rejects it.
4. Only then does the service touch the mailbox.

If the mailbox moved on in between — the person already filed it, another client moved it —
the suggestion is dead rather than applied to something it no longer describes.

The service also watches mailboxes itself and suggests things of its own. Same kind of thing,
same review. An agent's suggestion and the service's own are not treated differently.

## Who it is for

Several people sharing one deployment, each with their own mail accounts — IMAP, or Gmail
through its API.

## Why it is shaped this way

The review step is the whole point, so it has to be unavoidable. Agents get no way to apply
anything at all — not a permission they lack, but a capability absent from their side of the
service. Applying lives somewhere agents cannot reach.

## Where it goes later

Reviewing everything by hand does not scale and should not have to. Eventually a person
should be able to hand over standing authority within limits — an agent gets its own folder,
or a rule says filing mail from this sender needs no review.

Deliberately not in this version. But the review step is meant to be where that authority
plugs in, so adding it later is not a rebuild.

## Not in scope

Sending mail. Applying anything automatically. Protocols, storage, process layout — those
come with the components.

---

Partly derived from [`design-history/2026-08-12-mailmind-testability-and-ci-plan.md`](design-history/2026-08-12-mailmind-testability-and-ci-plan.md),
which is a starting point, not a decision. That document calls action suggestions "ideas".
