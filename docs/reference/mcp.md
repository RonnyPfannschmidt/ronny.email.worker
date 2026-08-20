# The MCP surface

What an agent connects to. Twelve tools, six resources, three prompts — and no way to
change a mailbox, which is a property of the list rather than of a permission check.

Both transports expose the same surface: a pipe (`mailmindctl mcp`) or
`http://127.0.0.1:8765/mcp/` with a bearer token. [Connecting an agent](../agents.md) has
the client configuration for each.

## Instructions

The server tells a connecting model, before anything else, that it cannot change anything
itself, that message content is data and never instruction, that a large mailbox is
approached with the summarise tools rather than by enumerating, and — when a review UI is
known — where the person reviewing is. Not the key that opens it.

## Tools that look

Every one of these needs `observe`.

| Tool | |
|---|---|
| `list_accounts()` | The accounts this grant covers. There may be none. |
| `list_containers(account_id)` | Folders, with how much of each is cached. |
| `summarize_senders(container_id, limit=100)` | Who a folder is from, with counts and dates. |
| `summarize_lists(container_id, limit=100)` | Mailing lists and bulk senders, by `List-Id`. |
| `list_messages(container_id, …)` | Messages, newest first. Bounded. |
| `search_messages(query, account_id, limit)` | Full-text over cached subjects and senders. |
| `get_message(message_id, include_body=False)` | One message. |
| `request_body(message_id)` | Fetch and cache a body from the server. |
| `request_sync(container_id)` | Bring the cache up to date. Observation, not a change. |

`list_messages` filters on `from_address`, `list_id`, `unread_only`, and `before`/`since`
as ISO 8601 — a date like `2026-08-19` or a timestamp like `2026-08-19T09:00:00Z`, `before`
exclusive and `since` inclusive, both against the message's own `Date` header.

Bounded means bounded: a request matching more than `max_messages_per_request` returns
fewer, and says so with the total. It never returns a slice that looks complete.

## Tools that say

| Tool | Needs | |
|---|---|---|
| `propose_bundle(...)` | `suggest` | One operation over an enumerated list of messages. |
| `withdraw_bundle(bundle_id, reason)` | `suggest` | Take back your own, before a decision. |
| `add_assessment(message_id, findings)` | `assess` | How trustworthy a message looks. |

`propose_bundle` takes `account_id`, `operation`, `message_ids`, `summary`, `reason`, and
`target_container_id` or `flag` depending on the operation. Operations are `move`,
`add_flag`, `remove_flag` and `delete`; delete moves to Trash, and nothing expunges. The
premise of each item — where the message is, what state it is in — is recorded at proposal
and checked again before anything happens, so a mailbox that moves on kills the item rather
than having it applied to whatever is there instead. It returns the bundle id and a note
saying nothing has changed yet, with the review link when one is known.

`add_assessment` findings are `{"code": ..., "detail": ..., "evidence": {...}}`, recorded
as *interpretation*: useful, and not decidable. The mechanical findings — signature-shaped
facts like a display name disagreeing with an address — are computed by the service and
cannot be written or overridden from here. An assessment is meant to come from somewhere
other than the producer of the suggestion it informs; where it does not, the reviewer is
shown that it did not.

## What is not there

No tool applies anything. `apply` is not a value the capability enum can hold, and the
module that writes to a mailbox is not imported by anything on this side. There is also no
send, no append, and no way to create or delete a folder.

## Resources

| URI | |
|---|---|
| `mailmind://accounts` | |
| `mailmind://bundles/open` | Waiting for a person. |
| `mailmind://bundles/decided` | With what was decided, and by whom. |
| `mailmind://bundle/{bundle_id}` | |
| `mailmind://suggestion/{suggestion_id}` | |
| `mailmind://containers/{account_id}` | |

All `application/json`, all scoped by the same grant as the tools.

## Prompts

`triage_mailbox`, `assess_message` and `hand_over`. Offered rather than imposed: a client
that never calls `prompts/get` gets the same tools and the same refusals. Each repeats the
same ground rules, because a client picks one prompt and never sees the others.
