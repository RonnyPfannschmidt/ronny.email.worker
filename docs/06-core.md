# 06 — The core

The part that keeps suggestions, review, and actual mailbox state from disagreeing with each
other.

> First cut, and the least settled of these. Written to be argued with.

## The problem it exists for

A suggestion is computed against a mailbox at some moment. It is reviewed later and applied
later still. In between, anything can happen — the person files the message, another client
moves it, the server renumbers everything.

So the core's job is narrow: know what a suggestion assumed, and notice when that stopped
being true.

## Premise and staleness

Every suggestion records what it was computed against. Before it is shown, and again before it
is applied, that gets checked. If it no longer holds, the suggestion is dead — not applied to
whatever happens to be there now.

The check has to happen twice because both gaps are real: proposing to reviewing, and reviewing
to applying. The second is the dangerous one, because a person has already said yes.

## Keeping a record

What happened, in order, kept rather than overwritten: what was suggested, what was assessed,
what a person decided, what was actually done to the mailbox. Partly because losing that is
losing the ability to explain what happened to someone's mail, partly because "who accepted
this" is the question that matters after something goes wrong.

The design-history document goes further and proposes an append-only event log with all state
derived from it, plus a deterministic drive mode for testing. That may be right, and it may be
more machinery than this needs. Deciding by building something smaller first seems better than
deciding now.

## Meets

- [02 — Action suggestions](02-action-suggestions.md) — holds their premise
- [03 — The review step](03-review.md) — tells it when something has gone stale
- [04 — Mailbox access](04-mailbox-access.md) — where staleness is actually observed

## Open questions

- **Is an event log the right shape, or is a plain database with an audit trail enough?**
  The event log buys deterministic replay and testability; it costs a lot of structure for a
  service whose write volume is one person's mail.
- What actually has to be consistent with what? Possibly less than it seems.
- Is a single writer necessary, or just convenient?
- How is staleness detected on backends that cannot tell you what changed?
- Does the record ever get pruned, and what does deleting mail mean for a log that mentions it?
