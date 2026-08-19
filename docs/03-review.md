# 03 — The review step

Where a person decides. The one step that cannot be skipped, and therefore the one that
decides whether the whole thing is worth using.

> First cut. Written to be argued with.

## What a reviewer has to see

The **effect**, not the intent. Which messages, where they are now, where they would end up.
A summary can sit alongside it, but accepting from the summary alone would make the review
theatre.

Beyond that: who suggested it and why, the [assessment](02-action-suggestions.md) of the mail
it is based on, and — where the action is a draft — the recipients and where each of them came
from. For a follow-up, what doing it would disclose.

## Accept, reject, or it goes stale

Acceptance is per suggestion and deliberate. No bulk accept over things not looked at, no
default that happens if you do nothing.

Rejection is as easy as acceptance, and can carry a reason. Suggestions nobody gets to should
expire rather than accumulate forever.

A suggestion whose premise has moved on cannot be accepted at all — the reviewer is told what
changed instead.

## A bundle is large when the action is one action

Homogeneity is what makes a bundle reviewable, not its length. One operation and one
target over an enumerated list stays readable at a size the same list would not be if each
item could do something different: a hundred messages moving to Archive is one decision
shown a hundred times, and a hundred messages each doing their own thing is a hundred
decisions dressed as one.

So a size limit guards against a bundle nobody can render rather than one nobody can
understand, which makes it a deployment setting rather than a design one — see
[11](11-deployment-and-identity.md).

## Where the lifecycle lives

Proposed → accepted → applied, with rejected, withdrawn, superseded and stale as the ways out.
It belongs here because acceptance is the only transition that matters; the rest are
bookkeeping around it.

## Where standing authority plugs in later

A rule that says "filing mail from this sender needs no review" is a thing that produces the
same acceptance the person would have produced. If that holds, adding it later changes who
accepts, not what acceptance is.

## Meets

- [02 — Action suggestions](02-action-suggestions.md) — what is being reviewed
- 06 — The core — decides when a suggestion has gone stale
- 08 — Untrusted content — everything shown to the reviewer came from mail

## Open questions

- If homogeneity is what carries a large bundle, what enforces it beyond one operation and
  one target? Two hundred messages to Archive is one decision; two hundred messages to
  Archive *for two hundred different reasons* may not be.
- **The real one:** what stops this becoming rubber-stamping? A queue that is fifty deep gets
  approved without reading, and then the boundary is decorative. This may be the constraint
  the rest of the design has to serve.
- Does review happen on a phone? If so, most of the above needs rethinking.
- Should suggestions be grouped — by sender, by kind, by thread — and does grouping reintroduce
  bulk acceptance through the back door?
- How long is a suggestion worth keeping before it expires?
