# Action suggestions

An agent has to be able to say what it thinks should happen without being able to make it
happen. An action suggestion is that: the unit the rest of the service is built around.

A sketch of its surface — what it is made of, and who may do what to it. How any of it works
is left for experiments to settle; see [open questions](#open-questions).

## Roughly, a suggestion holds

- what it concerns — an account, some messages
- what it would do — concretely, not as a description
- what the mail seems to be about, as the producer read it
- why it proposes this
- how trustworthy the mail looks — which does **not** come from the producer
- what it was computed against, so we can tell when it no longer holds

The firm part is the assessment not coming from the producer. If mail can steer an agent, it
must not also steer the reviewer's trust in that agent's suggestion.

## Three kinds of action

- **state** — flag, move, label, delete. Moves existing mail around.
- **draft** — create or edit a draft. Introduces new text, which is a different risk: it can
  put words in someone's mouth and address them to whoever it names. An accepted draft lands
  in the drafts folder and the person sends it themselves, so the service still does not send.
- **follow-up** — work the suggestion needs that its producer must not do itself.

## Follow-ups point two ways

*Outward*: fetch a document the mail refers to, ask an external service. Often a suggestion is
not decidable from the mail alone.

*Inward*: assess the mail. Reading it carefully enough to judge it is exactly what the
producer must not do on its own behalf.

One mechanism for both. A request says what is to be done, to what, and which configured app
does it; the app starts from that and returns a result.

The service asks for a basic assessment itself, on every suggestion, so it does not depend on
a producer remembering to. A producer may ask for a more specific one, choosing from
configured kinds rather than wording it, and what comes back adds to the basic one.

## Who may do what

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

## Choices that would be expensive to reverse

**Assessment is a follow-up, not a subsystem.** One mechanism for work a producer must not do
itself, pointed inward or outward. Cheaper than two, and it means the rule keeping a document
fetch away from the mailbox is the same rule keeping an assessment away from its producer.

**A grant comes from the follow-up request.** The request already names the app, the thing,
and the purpose, so nothing extra has to be invented to say what the app may touch. It ends
with the run.

**Some of an assessment must not need a model.** Signature checks, display name against parsed
address, invisible characters, link text against target — decidable, testable, and not the
app's to write. An app can be talked into a wrong reading; it should not be able to report a
valid signature. Where exactly this line falls is an experiment.

## Known costs

- A model provider is a third party that sees the mail. Not mitigated by anything here.
- Assessment on the path to review means a stalled assessor stalls the service.
- A basic assessment on every suggestion is the cost that scales with however much mail
  arrives.

## What this changes about the service

- Drafts mean a suggestion can add content to a mailbox, not only move existing mail. Sending
  stays out.
- Follow-ups give the service an outward-facing side, reaching networks and third parties on
  the say-so of mail nobody asked for. That is a bigger change to what this is than drafts are.
- Assessment sits between proposing and reviewing, so those are now ordered.
- The service becomes somewhere apps are configured and credentials minted for them.

## Open questions

Roughly in the order they need answering, and most want an experiment rather than an argument.

- What is in the basic assessment? It is the only one most suggestions get, and it runs on all
  of them. Worth having and cheap are in tension.
- One assessment pass or several? Separate passes per concern are more legible and testable;
  one pass sees the whole message.
- Is an assessment ever redone? A thread that turns out badly is evidence about messages
  already assessed — but redoing it means something a reviewer saw can change underneath them.
- Does the service's own suggestion-finding read mail directly, or only what has been assessed?
- How much does a suggestion have to carry for review to be possible without re-reading the
  mail?
- Does a self-hosted model change the third-party answer enough to matter?
