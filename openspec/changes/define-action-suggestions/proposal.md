## Why

An agent has to be able to say what it thinks should happen without being able to make it
happen. A suggestion is that: the unit the rest of the service is built around.

This is a sketch of its surface. What it is made of, and who may do what to it. How any of
it works is left for experiments to settle.

## What Changes

### Roughly, a suggestion holds

- what it concerns — an account, some messages
- what it would do — concretely, not as a description
- what the mail seems to be about, as the producer read it
- why it proposes this
- how trustworthy the mail looks — which does **not** come from the producer
- what it was computed against, so we can tell when it no longer holds

The firm part is the assessment not coming from the producer. If mail can steer an agent, it
must not also steer the reviewer's trust in that agent's suggestion.

### Three kinds of action

- **state** — flag, move, label, delete. Moves existing mail around.
- **draft** — create or edit a draft. Introduces new text, which is a different risk: it can
  put words in someone's mouth and address them to whoever it names. An accepted draft lands
  in the drafts folder and the person sends it themselves, so the service still does not send.
- **follow-up** — work the suggestion needs that its producer must not do itself.

### Follow-ups point two ways

*Outward*: fetch a document the mail refers to, ask an external service. Often a suggestion
is not decidable from the mail alone.

*Inward*: assess the mail. Reading it carefully enough to judge it is exactly what the
producer must not do on its own behalf.

One mechanism for both. A request says what is to be done, to what, and which configured app
does it; the app starts from that and returns a result.

The service asks for a basic assessment itself, on every suggestion, so it does not depend on
a producer remembering to. A producer may ask for a more specific one, choosing from
configured kinds rather than wording it, and what comes back adds to the basic one.

### Who may do what

| | reads mail | reaches outside | writes mailbox |
|---|---|---|---|
| Producer | yes | no | no |
| Assessing app | the message it was given | no | no |
| Fetching app | only the reference it was given | yes | no |
| Applier | no — only resolved operations | no | yes |

No row has two of the three. "Agents cannot apply" is a consequence of that rather than a
rule of its own.

A follow-up request is also what a credential is minted from — it already names the app, the
thing, and the purpose. Apps are configured up front with what they are for. The mechanics
are an operations question.

## Capabilities

### New Capabilities

- `action-suggestion`: what a suggestion is made of and its kinds of action.
- `follow-ups`: the one mechanism for work a producer must not do itself.
- `capability-separation`: reading mail, reaching outside, writing the mailbox — who holds
  which.

Specs are not written yet. The lifecycle, the review step, and what an assessment actually
reports all wait on experiments.

### Modified Capabilities

None. `openspec/specs/` is empty.

## Impact

- Drafts mean a suggestion can add content to a mailbox, not only move existing mail. Sending
  stays out.
- Follow-ups give the service an outward-facing side, reaching networks and third parties on
  the say-so of mail nobody asked for. That is a bigger change to what this is than drafts are.
- Assessment sits between proposing and reviewing, so those are now ordered.
- The service becomes somewhere apps are configured and credentials minted for them.
