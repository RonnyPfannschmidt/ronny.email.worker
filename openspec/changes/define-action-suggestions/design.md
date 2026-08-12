## Context

A suggestion carries an assessment, and the assessment must not come from the producer —
otherwise mail that steered an agent also steers the reviewer's trust in it. Something else
has to make that judgement, and making it requires reading the mail carefully.

The capability split in the proposal says whatever reads hostile content can do nothing else
— no tools, no network. This document is about what makes that true rather than merely
stated.

## Goals / Non-Goals

**Goals:**

- Assessment means the same thing regardless of which producer raised the suggestion.
- The assessor's inability to act is a property that holds, not a claim someone makes.
- Assessment is reproducible enough to test against the adversarial corpus.
- A person can still point their own agent, with their own prompt, at the service.

**Non-Goals:**

- Choosing a model, a provider, or a prompt format.
- The mechanics of token issuance — see "Operations" below.
- Preventing a person from running a badly-prompted agent against the service. That is
  their business; the boundary is what protects the mailbox from it.

## Decisions

### Assessment is a follow-up, not a subsystem

A producer proposes. Anything the proposal needs that the producer must not do itself is a
follow-up — and reading the mail carefully, in order to judge it, is exactly such a thing.

So there is one mechanism, not two. Fetching a referred document is a follow-up. Asking an
external service is a follow-up. Assessing the mail a suggestion is about is a follow-up. It
is the inward-facing one, and it differs from the others only in what it is pointed at and
what it is allowed to touch.

This is why the producer cannot assess its own suggestion in the same breath: a follow-up is
carried out by something else, running under its own grant. The rule that already kept a
document fetch away from the mailbox is the rule that keeps an assessment away from the
producer.

### A follow-up request is the context a credential is minted from

A follow-up request already says what is to be done, to what, and which configured app does
it. That is what a token needs — a subject, a resource, a scope, and a lifetime that ends
with the run. Nothing further has to be invented: the request *is* the authorization
request, and a configured app starts from it.

| The request names | Becomes |
|---|---|
| Which app | the subject |
| What it concerns | the resource |
| What it is for | the scope |
| This one run | the lifetime |

An assessment follow-up names a message, so the grant it produces names that message. The
app can read it, receive its mechanical findings, see thread and correspondent context as
metadata, and write one assessment for it. It cannot reach another message, cannot reach the
network, cannot write to the mailbox, and cannot create a suggestion.

Because the grant is minted from the request and dies with the run, the isolation is not a
convention about tool lists. A steered assessment app holds authority over the one message
it was already assessing, and over nothing else. It cannot leave a note for a later run to
read, which is what would rebuild a channel between messages.

### A baseline assessment is automatic; further ones can be asked for

The service requests a baseline assessment itself, for every suggestion, without being
asked. A producer cannot decline it, cannot influence it, and does not have to remember it.
That is what makes assessment unskippable: if asking were the producer's job, a producer
that never asks would be a producer whose suggestions are never judged, and a steered one
would learn not to ask.

On top of that, a producer may request further assessments — narrower questions about the
same mail, where it has reason to think a closer look is warranted. Three things keep that
from being a way back in:

- A request **selects a configured kind**; it does not supply instructions. An agent that
  could word the assessment would be writing the judgement of its own suggestion, which is
  the laundering the split exists to prevent.
- Further assessments **only add**. They cannot remove, replace, or contradict a baseline
  finding, and the reviewer sees the baseline whatever else was asked for.
- Each is **attributed to whoever asked**, so a reviewer can tell the service's own baseline
  from something the producer went looking for.

Asking costs a run, so the number a producer may ask for is bounded like anything else it
consumes.

### Apps are configured, and the registration is what bounds them

A follow-up is carried out by a configured app, not by an arbitrary caller. Registration
declares what an app is for and the shape of grant it may be given, and the service will not
mint outside that shape however a request is worded.

This is where the capability table stops being an intention. Read mail content, reach
outside, write the mailbox — no registration may claim two of the three. An assessment app
reads and does not reach out; a fetching app reaches out and is given the reference and the
stated context rather than the message. The rule is checked once, against a registration,
rather than argued about per request.

### Mechanical findings are not part of any of this

Some of what an assessment reports needs no model and must not depend on one: authentication
results, display name against parsed address, characters present but not rendered, link text
against link target, attachment facts, whether this correspondent or recipient set is new.

| | produced by | deterministic | required |
|---|---|---|---|
| Mechanical findings | service code, no app, no grant | yes | yes |
| Interpretation | a configured assessment app | no | no |

Findings are computed by the service and handed to the app as input. It cannot determine
them and cannot contradict them. That is what makes a steered assessment survivable — the
app can be talked into a wrong reading of a message; it cannot be talked into reporting a
valid signature.

### An agent pointed at the service over MCP supplies its own prompt

The service neither supplies nor inspects it. That agent is a producer, subject to the
boundary and to an assessment it does not control and cannot skip. Its prompt determines what
it suggests, never what the suggestion is worth.

## Operations

Issuance mechanics are an operational concern, not a design one. Whether people authenticate
against an external provider while the service mints its own workers' credentials, whether a
narrow grant comes from token exchange or from an issuer that mints to order, what the
lifetimes are, and how issuance keeps up at mail volume — all of that is deployment shape.
The design constrains it in one way only: a grant must be derivable from a follow-up request
and must not outlive the run.

## Risks / Trade-offs

- **A steered assessment app writes a misleading interpretation.** → Mechanical findings are
  not the app's to write, and the reviewer sees both halves distinctly.
- **The service depends on a model** — spend, rate limits, provider outages. → Only
  interpretation depends on it; mechanical findings and the boundary keep working without one.
- **Model spend scales with mail volume, driven by whoever sends it.** → One bounded run per
  follow-up caps the per-message cost, and mechanical findings can gate which mail is worth
  interpreting at all.
- **Prompts become a maintenance surface with no compiler.** → Versioned with the code and
  exercised by the corpus, which therefore has to cover the interpretive half too.
- **A model provider is a third party that sees the mail.** → Real, and not mitigated by
  anything here. A deployment decision that deserves its own change rather than being settled
  as a side effect of this one.
- **Assessment is now on the path between proposing and reviewing.** → A stalled or failing
  assessment app leaves suggestions that cannot complete. Needs a visible backlog and a
  stated behaviour when it grows.
- **Scope names are a contract across components.** → Defined once and versioned; a scope
  meaning something slightly different to two components is an authorization bug that reads
  like a naming problem.

## Open Questions

- Is one assessment app enough, or does each concern — spoofing, injection, links, tone —
  want its own follow-up with its own prompt? Separate runs are more legible and more
  testable; one run is cheaper and sees the whole message at once.
- Is an assessment ever redone? A thread that turns out badly later is evidence about
  messages already assessed, but redoing means an assessment a reviewer saw can change under
  a suggestion that cited it.
- What is in the baseline? It has to be worth having on its own, since it is the only
  assessment most suggestions will get, but it runs on every suggestion and so is the thing
  whose cost is multiplied by mail volume.
- Does the service's own suggestion-finding read mail directly, or only what has already been
  assessed? The second is safer and narrower; the first is what lets it notice something no
  assessment was looking for.
- Does a self-hosted model change the third-party answer enough to matter, and should the
  service be specified so that it can be one?
