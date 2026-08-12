# 05 — The agent surface

What an agent connects to, over MCP. What it can see, what it can say, and what is simply
not there.

> First cut. Written to be argued with.

## Look and say

Two things an agent can do: observe mail it has been given a view of, and make
[action suggestions](02-action-suggestions.md) about it. It can also ask for a follow-up —
fetch something the mail refers to, or a closer assessment — choosing from configured kinds
rather than describing what should happen.

There is no third thing. Applying is not a permission an agent lacks; it is a capability that
does not exist on this side of the service.

## The view is given, not chosen

What an agent can see comes from its grant, not from what it asks for. An agent cannot widen
its own scope, cannot name a tenant, and cannot assert who it is — all of that is settled
before it says anything.

Observation is bounded. A request that would return more than the limit gets less, and is told
so, rather than silently returning a slice that looks complete.

## Its prompt is its own business

A person points their own agent at the service, with their own prompt. The service neither
supplies nor inspects it. That agent's prompt decides what it suggests; it never decides what
the suggestion is worth, because assessment does not come from the producer.

## Meets

- [02 — Action suggestions](02-action-suggestions.md) — what it produces
- [03 — The review step](03-review.md) — where what it produces goes
- 07 — Tenancy — the grant binds it to one person's mail

## Open questions

- **What does an agent actually need to be useful?** Everything here is a guess until something
  real is pointed at it. Too narrow and it cannot form a sensible suggestion; too wide and the
  bounded-observation rule is doing nothing.
- Search, enumerate, or both? Enumeration over a large folder is where the limits will bite.
- Does an agent see message bodies by default, or metadata until it asks?
- Should an agent see its own past suggestions and how they were decided? Useful for not
  repeating rejected ones; also a channel for a steered agent to learn what gets through.
- What does an agent see about assessments — the findings, or only that one exists?
