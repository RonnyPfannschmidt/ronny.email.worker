## Why

The rough definition says agents produce *action suggestions* and a person accepts them.
That only works if a suggestion carries enough for the person to actually judge it — which
is more than a list of operations. It needs to say what the mail was understood to be, and
what the service thinks of the mail it read.

This is the first component. Everything else attaches to it.

## What Changes

### What a suggestion is made of

| Part | What it holds | Who produces it |
|---|---|---|
| Scope | The account and the messages or threads concerned | producer |
| Action | What would change — see below | producer |
| Reading | What the producer understood the mail to be about | producer |
| Grounds | Why it proposes this, pointing at the content that led there | producer |
| Assessment | What was found about the trustworthiness of that mail | **a follow-up, not the producer** |
| Premise | The mailbox state it was computed against | service |

The split on the last two rows is the load-bearing one. A producer says what it wants and
why. It does **not** get to say the mail is safe. If an agent could attach its own clean
bill of health, mail that successfully steered the agent would also steer the reviewer's
judgement of it — the assessment has to come from somewhere the mail's author cannot
influence through the agent.

Which is to say the assessment is not present when a suggestion is first made. It arrives as
a follow-up, described below.

### Three kinds of action

**State actions** move existing mail around: flag, move, label, delete. They do not create
content. The mail is already in the mailbox; the suggestion is about where it sits.

**Draft actions** create a draft, or edit an existing one. These introduce new text. That
makes them a different risk: a state action can misfile mail, but a draft action can put
words in the person's mouth, and can address them to whoever the draft names.

A draft that is accepted lands in the drafts folder. The service does not send it. Sending
stays where it is today — the person's own mail client, deliberately outside this service.
So "no outbound mail" from the rough definition still holds, and a draft suggestion is a
proposal for something the person will send themselves, later, on purpose.

**Follow-up actions** are work a suggestion needs that its producer must not do itself. They
change no mail. Two directions:

*Outward* — fetch a document a message refers to, read it, ask an external service
something. These exist because a suggestion is often not decidable from the mail alone: the
mail refers to an invoice, a ticket, a shared document, and whether the suggestion is right
depends on what that says.

*Inward* — assess the mail the suggestion is about. Reading mail carefully enough to judge
it is precisely something the producer must not do on its own behalf, so it is a follow-up
like any other. This is where the assessment in the table above comes from.

A basic assessment is requested by the service itself, on every suggestion, so it does not
depend on a producer choosing to ask. A producer may additionally ask for a specific one
where it has reason to want a closer look — but it picks from configured kinds rather than
saying what the assessment should conclude, and what comes back adds to the basic assessment
instead of replacing it.

One mechanism covers both. A follow-up says what is to be done, to what, and which
configured app does it; the app runs from that and returns a result. What separates the
assessment case from the fetching case is only what it is pointed at and what it is allowed
to touch.

### What the assessment covers

The mail, and its surroundings — the thread it sits in, the sender's history, the other
recipients, and what the message carries:

- whether the sender is who they appear to be, from parsed addresses and authentication
  results rather than display names
- whether the content tries to instruct its reader
- content that is present but not visible when rendered
- links whose visible text differs from their target, and remote references
- attachments
- whether this correspondent, thread, or recipient set is new

The assessment is attached to the suggestion, not stored as a verdict about the message.
Two suggestions about the same mail carry their own assessments, made when they were made.

### What a draft suggestion has to expose

More than its text, because the dangerous parts of a draft are not in the prose:

- every recipient, and **where each one came from** — a header on the message being replied
  to, an address found in a body, or one the producer supplied itself
- what it quotes from other mail
- for an edit, what changes against the current draft, not just the resulting text

A recipient the producer invented, or one lifted from the body of a message rather than its
headers, is the shape an exfiltration attempt takes. The reviewer sees the provenance of
each recipient for that reason.

### Why a follow-up is a separate actor

Whatever reads hostile mail must be the thing least able to do damage with it. So the part
that reads and assesses a message has no tools, no network, and no way to reach an external
service — not because it has nothing useful to do with them, but because that is what makes
it safe to point at dangerous content. A follow-up therefore cannot be carried out by the
thing that wanted it. It is requested, and something else does it.

That splits the service by capability rather than by trust level:

| | reads mail content | reaches outside | writes mailbox |
|---|---|---|---|
| Producer (suggesting agent) | yes | no | no |
| Assessment app — inward follow-up | one named message | no | no |
| Fetching app — outward follow-up | only the reference and stated context | yes | no |
| Applier | no — only resolved operations | no | yes |

No row has two of the three. That is the actual rule; "agents cannot apply" is one
consequence of it.

The two follow-up rows are the same mechanism pointed in opposite directions, and neither
gets the combination that matters. The outward one is given the reference and the context
the suggestion states, not the mailbox — handing it the message would put
reading-hostile-content and reaching-outside in the same place. The inward one gets a
message but no way off the machine.

### What a follow-up request has to expose

A follow-up reaches outward on the strength of something an unknown person wrote, so what
leaks matters as much as what is fetched:

- what would be disclosed by doing it — fetching a URL tells whoever controls it that the
  mail was read, from where, and when, and a URL can carry an identifier that says which
  message and which person
- where the reference came from — a header, the body, an attachment, or the producer's own
  invention, the same provenance question drafts raise about recipients
- what the external service would be told, and whether the request changes anything on the
  far side or only asks

### Results come back as untrusted content

Whatever a follow-up returns is a document written by someone unknown, arriving because a
message asked for it. It is assessed on the way in, exactly as mail is. Otherwise a fetched
document is a way to say to the producer what the message was not allowed to say directly.

Results become grounds for a new or revised suggestion. They do not silently upgrade the
suggestion that asked for them.

### Where the thinking happens

Two different things use a model, and they are not the same kind of thing.

**Configured with the service.** The apps that carry out follow-ups are agents, and the
service configures them: what each is for, and the shape of grant it may be given. A
follow-up request supplies the rest — what to work on, for this one run — so an app starts
from the request and holds authority over nothing beyond it. Their prompts belong to the
service too, versioned with it and exercised by its tests, so what an assessment means does
not drift. The service's own suggestion-finding works the same way.

**Pointed at the service.** A person can aim their own agent, carrying their own prompt, at
the service over MCP. That agent is a producer of suggestions like any other and gets no
more than the agent surface allows. Its prompt is its own business; the service neither
supplies nor inspects it.

Both are agents; the difference is who holds the leash. The first is part of what the
service *is*, so its toolset and its prompts are the service's. The second is deliberately
open-ended, and is kept harmless by the boundary rather than by its contents. See
[`design.md`](design.md) for why the first is run rather than merely defined.

### Interaction with the other components

- **review** shows the suggestion; the assessment, the draft provenance, and what a
  follow-up would disclose are what it has to render, so the shape here fixes what review
  must display
- **untrusted content** supplies the mechanical findings an assessment builds on, for mail
  and for follow-up results alike
- **mailbox access** decides whether an action is expressible against a given backend
- **the core** holds the premise and decides when a suggestion no longer describes reality
- **the agent surface** is one grant among several rather than a special case — a
  general-purpose agent simply gets a broader one than a follow-up app does

## Capabilities

### New Capabilities

- `action-suggestion`: what a suggestion is made of, its three kinds of action, and the
  rules about who may assert what within it.
- `follow-ups`: the one mechanism for work a producer must not do itself, inward and
  outward, and what a request has to say.
- `capability-separation`: the split between reading mail, reaching outside, and writing to
  a mailbox, expressed as which grant an app may be configured for.

Suggestion lifecycle — proposed, accepted, applied, stale — is deliberately left to a later
change, together with the review step it belongs to. This change fixes what a follow-up
request must say, not the mechanics of issuing credentials from it.

### Modified Capabilities

None. `openspec/specs/` is empty.

## Impact

- Reopens a line in the rough definition: drafts mean a suggestion can add content to a
  mailbox, where that change listed only moving existing mail. Sending remains out.
- Makes `untrusted-content` a dependency rather than a later concern, since no suggestion is
  complete without an assessment.
- Follow-ups give the service an outward-facing side the rough definition did not have. It
  now reaches networks and third-party services, and does so on the say-so of mail it did
  not solicit. That is a larger change to what the service is than drafts are.
- Assessment moves onto the path between proposing and reviewing, rather than sitting beside
  it. A suggestion is made first and judged after, so the two are ordered where before they
  were merely both required.
- The service becomes a place apps are configured and credentials are minted for them. That
  is an operational surface the rough definition did not have, and it is where the capability
  split is actually enforced.
