# Identifiers

A message has three kinds of id, made by three different parties for three different
reasons. Most confusion about mail identity is one of them being asked to do another's job.

| | Made by | Unique | Survives |
|---|---|---|---|
| `Message-ID` | whoever composed the mail | **no** | everything — servers, accounts, moves |
| IMAP `UID` | the server, per folder | within a folder and UIDVALIDITY | nothing that moves the message |
| `message_id` | mailmind | yes, per database | syncs, moves, expunges |

The short version: **an agent uses mailmind's `message_id` and nothing else.** The other two
exist below it and are the service's business.

## `Message-ID` — the sender's id

Assigned once by whatever composed the mail, and travels with it forever: across servers,
across accounts, into everybody's copy. It is what `In-Reply-To` and `References` thread on,
which is the job it is good at.

It is *semi-unique*, and that is not a defect to be worked around — it is what the header
is. Stable enough to reply to, not unique enough to key on:

- a forward or a resend may carry the original's `Message-ID` while being different mail;
- one mailbox may hold several genuinely distinct copies that share one;
- plenty of mail has none at all.

Measured on one real mailbox of 29,081 messages (2026-08-25): 520 header values were shared
by 1,070 rows, and 121 further messages had no `Message-ID` at all. The 520 break down as

- **170** where the subject or the sender differed — different mail carrying one header,
  such as a forward that kept the original's;
- **193** where two *live* messages sat in the **same folder** under different UIDs and
  different sizes — genuinely two copies, each separately addressable;
- **157** where the same mail was filed in more than one folder.

Only the last is what somebody means by "the same message twice", and it is a minority of a
minority. Keyed on `Message-ID`, the first two would have been merged into one row and the
121 would have had no name at all.

mailmind stores it as `message.message_id_header` and uses it as **one ingredient of
`content_key`**. Nothing keys on it alone, and nothing should.

## IMAP's ids — the server's handles

Three, of which mailmind uses two.

| | What it is | mailmind |
|---|---|---|
| sequence number | the message's position in the folder, renumbered on every expunge | **never used** |
| `UID` | unique within a folder, ascending, never reused | `placement.uid` |
| `UIDVALIDITY` | the folder's identity — change it and every UID means something else | `container.uidvalidity`, as `container.generation` |
| `MODSEQ` | a change counter (CONDSTORE) | `placement.modseq`; incremental sync, and `UNCHANGEDSINCE` on apply |

Sequence numbers are absent on purpose. They shift under you as soon as anything is
expunged, so anything remembered in terms of them is wrong by the time it is used.

**A UID means nothing without its folder and its UIDVALIDITY.** That is why a placement
records `container_generation` beside the UID, and why a message moved between folders gets
a *new* UID at the destination — the UID belongs to the folder, not to the mail.

## mailmind's ids — what the agent sees

- **`message.id`** — the durable handle, and the only one on the agent surface. Never
  reused, never deleted.
- **`message.content_key`** — a SHA-256 over `Message-ID`, subject, sender, date and size,
  unique per account. This is the *identity function*: the reason `message.id` stays put is
  that a sync looks a message up by this and finds the row it already has.
- **`container.id`** — a folder, unique per account and name.
- **`placement.id`** — one message, in one container, at one UID, under one generation.

Placements are never deleted. A message that leaves a folder gets `gone_at` set instead, so
a suggestion pointing at it can still say what happened rather than dangle.

## What changes when

| Event | What happens to `message_id` |
|---|---|
| incremental or full sync | nothing — the row is found by `content_key` |
| message moved between folders | nothing; old placement `gone_at`, new placement, new UID |
| message expunged | nothing; the row and its id outlive the mail |
| folder recreated (UIDVALIDITY changes) | nothing; the generation moves on and suggestions resting on it are killed |
| **the cache rebuilt from scratch** | **everything is renumbered** |

Only the last one moves ids, and it is the answer whenever ids appear to have shifted. A
UIDVALIDITY change is handled by `break_identity`: the generation is bumped, every placement
under the old one is marked gone, and every suggestion resting on one is marked stale —
because the alternative is applying it to whatever now happens to sit at that UID. The
reasoning is in [06 — The core](../design/06-core.md).

## One mail, two ids

`content_key` is computed from *parsed* fields, size among them. So if the server reports a
different size for what a person would call the same mail — a header rewritten in transit,
or a change in what the parser extracts — it becomes a second row with a second id.

This is the only way one mail acquires two ids without being two messages, and from inside
the database it is indistinguishable from the ordinary case of a folder genuinely holding
two copies. What tells them apart is the server: two copies have two live UIDs.
