# 07 — Tenancy

Several people share one deployment. Nothing of one person's ever reaches another.

> First cut. Written to be argued with.

## What has to hold

Every account, suggestion, assessment and record belongs to exactly one person, decided when it
is created and never afterwards. No suggestion spans two of them. Nobody reviews another
person's mail.

Agents and follow-up apps are bound to a person by the grant they run under — never by
something they say in a request.

## Where it should be enforced

Below the code that queries, not inside it. A query that forgets its filter should return
nothing rather than everything, because that mistake will be made eventually and the cost of it
here is somebody else's mail.

Whether that means a database that enforces it, a separate database per person, or something
else, is exactly the kind of thing to decide by trying.

## Meets

- [05 — The agent surface](05-agent-surface.md) — a grant names one person
- [06 — The core](06-core.md) — the record is per person too

## Open questions

- **Is this needed now?** Single-user is much simpler, and retrofitting isolation later is the
  classic way to get it wrong. Doing it up front costs design effort on something that may never
  have a second user.
- Separate storage per person, or one store that enforces the boundary? The first makes the
  guarantee obvious and makes anything shared awkward.
- Shared mailboxes exist in real life — a support address two people watch. Does that break the
  model, or is it a person with two logins?
- One person with several accounts is normal. Is the boundary the person, or the account?
- Where do the service's own follow-up apps sit? They act for one person at a time but are
  configured once for everybody.
