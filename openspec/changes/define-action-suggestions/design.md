## Context

A sketch, not a design. It records the few choices that shape everything else, and what has
to be tried before the rest can be written down honestly.

## Goals / Non-Goals

**Goals:** fix the shape — what a suggestion is, and who is allowed to do what.

**Non-Goals:** models, prompts, protocols, storage, token mechanics, and anything whose right
answer depends on trying it.

## Decisions

Three, and they are the ones that would be expensive to change later.

**Assessment is a follow-up, not a subsystem.** One mechanism for work a producer must not do
itself, pointed inward or outward. Cheaper than two, and it means the rule keeping a document
fetch away from the mailbox is the same rule keeping an assessment away from its producer.

**A grant comes from the follow-up request.** The request already names the app, the thing,
and the purpose, so nothing extra has to be invented to say what the app may touch. It ends
with the run.

**Some of an assessment must not need a model.** Signature checks, display name against
parsed address, invisible characters, link text against target — decidable, testable, and not
the app's to write. An app can be talked into a wrong reading; it should not be able to report
a valid signature. Where exactly this line falls is an experiment.

## Risks / Trade-offs

- A model provider is a third party that sees the mail. Not mitigated by anything here.
- Assessment on the path to review means a stalled assessor stalls the service.
- A basic assessment on every suggestion is the cost that scales with however much mail
  arrives.

## Open Questions

Roughly in the order they need answering, and most want an experiment rather than an argument.

- What is in the basic assessment? It is the only one most suggestions get, and it runs on
  all of them. Worth having and cheap are in tension.
- One assessment pass or several? Separate passes per concern are more legible and testable;
  one pass sees the whole message.
- Is an assessment ever redone? A thread that turns out badly is evidence about messages
  already assessed — but redoing it means something a reviewer saw can change underneath them.
- Does the service's own suggestion-finding read mail directly, or only what has been assessed?
- How much does a suggestion have to carry for review to be possible without re-reading the
  mail?
- Does a self-hosted model change the third-party answer enough to matter?
