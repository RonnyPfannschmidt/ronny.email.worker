# Documentation

- [x] [01 — Intent](01-intent.md) `#sketch`
- [x] [02 — Action suggestions](02-action-suggestions.md) `#sketch` `#open-questions`
- [ ] 03 — The review step `#todo`
- [ ] 04 — Mailbox access `#todo`
- [ ] 05 — The agent surface `#todo`
- [ ] 06 — The core `#todo`
- [ ] 07 — Tenancy `#todo`
- [ ] 08 — Untrusted content `#todo`

Plus [design history](design-history/) — starting-point documents, kept verbatim, not
authoritative.

## Status tags

| Tag | Meaning |
|---|---|
| `#todo` | Not written yet |
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
