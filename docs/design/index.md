# Design notes

Why mailmind is shaped the way it is. These are working documents — sketches, arguments and
open questions, written to be argued with — and they are not how to use the thing. That is
[the guide](../index.md), which these pages sit underneath.

Numbering is order of introduction, not dependency.


- [x] [01 — Intent](01-intent.md) `#sketch`
- [x] [02 — Action suggestions](02-action-suggestions.md) `#sketch` `#open-questions`
- [ ] [03 — The review step](03-review.md) `#draft` `#open-questions`
- [ ] [04 — Mailbox access](04-mailbox-access.md) `#draft` `#open-questions`
- [ ] [05 — The agent surface](05-agent-surface.md) `#draft` `#open-questions`
- [ ] [06 — The core](06-core.md) `#draft` `#open-questions`
- [ ] [07 — Tenancy](07-tenancy.md) `#draft` `#open-questions`
- [ ] [08 — Untrusted content](08-untrusted-content.md) `#draft` `#open-questions`
- [x] [09 — Iteration one](09-iteration-one.md) `#tried` `#open-questions`
- [ ] [10 — Running it](10-running-it.md) `#sketch` `#open-questions`
- [ ] [11 — Deployment and identity](11-deployment-and-identity.md) `#sketch` `#open-questions`
- [ ] [12 — An agent of your own](12-an-agent-of-your-own.md) `#sketch` `#open-questions`
- [x] [13 — Logging an agent in](13-logging-an-agent-in.md) `#tried` `#open-questions`

Plus [design history](history/index.md) — starting-point documents, kept verbatim, not
authoritative.

## Status tags

| Tag | Meaning |
|---|---|
| `#todo` | Not written yet |
| `#draft` | A first cut, written to be argued with — expect it to be rewritten |
| `#sketch` | Surface described — what it is made of, who may do what to it |
| `#open-questions` | Carries questions that want an experiment before they can be answered |
| `#tried` | The shape has been built once and survived it |
| `#settled` | Specified in detail, because the detail is now known |

## How these are written

Sketches first. A component gets a document describing its surface — what it is made of, who
may do what to it, and how it meets the components around it. Details that depend on trying
something are recorded as open questions, not decided in advance.

Each document carries its own open questions. They get answered by experiment and folded back
in; a question that turns out to be structural rather than incidental usually means the sketch
was wrong somewhere above it.

Detailed specification comes after the shape has been tried, not before.

Numbering is order of introduction, not dependency. A later document may well change an
earlier one — when it does, the earlier one gets edited rather than contradicted.
