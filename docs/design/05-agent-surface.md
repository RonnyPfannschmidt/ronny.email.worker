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
so, rather than silently returning a slice that looks complete. That is one rule and therefore
one envelope: listing, searching and summarising all answer with `returned`, `total_matching`,
`truncated`, `next_cursor` and a `note`. They did not, for a while — search returned no total
and the summaries returned bare lists, which is exactly the slice that looks complete.

The limit is in bytes, not rows, because bytes are what truncates on the way to a client.
opencode cuts a tool result at around 50 KB; measured against that, a listing of fifty
messages weighed 41 KB, a folder list 66 KB, a bundle of four hundred items 208 KB and a
newsletter with its links 108 KB. The row limit noticed none of it, and three of those four
had no limit at all. Worse, the envelope used to sit *after* the rows, so the client's cut
took away precisely the fields that said the answer was incomplete and left JSON that no
longer parsed. The envelope leads now.

And what is left out is reachable. Every listing carries a cursor; handing it back continues
where the last answer stopped. This reverses something this note used to imply and the code
used to say outright — that paging past the observation limit is the same ask spelled
differently, a model asking for more than it can read. That argument holds for messages,
where narrowing by sender or list is a better answer and the note still says so. It does not
hold for the folder list, for the items of a bundle the agent itself proposed, or for the
record of what has been decided: none of those can be narrowed by anything, and all three
were simply unbounded. A rule with three surfaces outside it is not one rule.

Cursors are opaque and belong to the request that made them: change the folder, the filter or
the query and the cursor is refused rather than answering a different question from a
position that means nothing in it. They are keyset rather than offset wherever the ordering
has a column to resume from, because a mailbox is being synced underneath a listing and an
offset asked twice returns a window over a list that has shifted. Search is the exception —
its order is FTS5's relevance rank, which is not a column that can be compared — so it pages
by position and can repeat or miss a result when the index changes between pages. Said out
loud in the tool description, because a caller that thinks it enumerated a search exhaustively
is a caller about to propose a bundle it believes is complete.

## Naming mail, or finding it

An agent says what to change by naming the messages or by naming a search. A search is
resolved once, at the moment of proposing: what it finds becomes the enumerated list, and
the bundle never looks again. There is no standing query, because a bundle that re-ran its
own search between being read and being accepted would be a bundle nobody read.

The two ways of naming differ in what an unusable message means. An id is a claim about that
message, so one that has moved refuses the bundle. A search claims nothing about any one
message, so those are left out — and the answer says how many and why, the same promise the
bounded envelope makes about a listing.

A search matching more than a bundle may hold is refused with the number rather than trimmed
to fit. The most relevant few is a set nobody chose: not the agent, which asked for all of
them, and not the person, who cannot tell why these and not those.

Worth stating plainly: the index a search runs over is subjects, senders and previews, all
written by strangers. Membership of a bundle found by searching is therefore decided partly
by text somebody else controls, and a message can be written to fall into a predictable
search. What bounds it is unchanged — the reviewer reads the enumerated list, and the list
stays the primary thing on the page, with the search shown beside it rather than instead.

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
- Does an agent see message bodies by default, or metadata until it asks?
- Should an agent see its own past suggestions and how they were decided? Useful for not
  repeating rejected ones; also a channel for a steered agent to learn what gets through.
- What does an agent see about assessments — the findings, or only that one exists?
