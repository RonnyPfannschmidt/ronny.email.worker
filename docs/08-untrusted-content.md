# 08 — Untrusted content

Mail is written by strangers. Everything that reads it has to treat it as an attempt to steer
whoever is reading.

> First cut. Written to be argued with.

## Content is data, never instruction

Headers, bodies, attachment names, folder names. None of it changes what the service does, what
a suggestion would perform, or what a reviewer is shown about it. When mail is handed to a
model, it goes as clearly marked data, distinguishable from anything the service said.

Instruction-shaped text in a message is just text that happens to look like that.

## Two kinds of finding

**Mechanical** — decidable without a model, and therefore not something an agent can talk its
way around: whether the signature checks out, whether the display name matches the parsed
address, characters that are present but do not render, a link whose text disagrees with its
target, what the attachments are, whether this correspondent is new.

**Interpretation** — what a model adds. What the mail appears to want, whether it reads as
pressure, whether the ask fits the relationship. Useful and not decidable; marked as such.

An assessment can be talked into a wrong reading. It should not be able to report a valid
signature.

## Showing mail to a person

Rendering must not lie, and must not reach the network. Invisible content gets surfaced, a
link's real target is visible, remote images are not fetched — fetching one tells the sender
the mail was read.

Identity comes from parsed addresses. A display name is decoration and never identifies
anybody.

Mail that cannot be parsed is marked as such and kept out of reasoning, rather than being
silently treated as empty.

## Meets

- [02 — Action suggestions](02-action-suggestions.md) — assessments are built on the mechanical
  findings
- [03 — The review step](03-review.md) — everything a reviewer sees passes through here
- [04 — Mailbox access](04-mailbox-access.md) — where the raw material arrives

## Open questions

- **Is this a component at all, or part of mailbox access?** Parsing, findings and rendering all
  sit right where mail enters. A separate document may be describing a concern rather than a
  thing.
- What is the minimum set of mechanical findings worth having? Every one costs implementation
  and each is a place to be subtly wrong.
- Follow-up results arrive from outside and are equally untrusted — same treatment, or does a
  fetched document need its own?
- How much can be borrowed rather than built? Signature checking and MIME parsing are solved
  problems with sharp edges.
- The adversarial corpus in design history is the test suite for this. It may be the thing to
  build first, before anything it tests.
