# Reviewing

Where a proposal becomes a change, and the one step that cannot be skipped.

## Getting in

`mailmindctl serve` prints a link with the login key in it. Open it once: the key becomes a
session cookie and leaves the address bar.

`serve` prints it only to a terminal. Redirected, or run as a systemd unit, there is
nobody reading and something keeping, so the key stays in its file and stderr gets the path
instead — which is what the journal then shows.

When the UI belongs to a `mailmindctl mcp --serve` session, the link is **not** printed —
clients collect that process's stderr into a log, and some show the log to the model. It
goes to a file only you can read, and stderr gets the path. `mailmindctl review --open`
follows it.

A restart mints a new key, so an open tab stops working and you go back to the terminal.

## The queue

What is waiting, for the account you are working in. Each entry is a bundle: one operation
over an enumerated list of messages, with the summary and reason its producer wrote.
`mailmindctl status` answers the same question without a browser.

## A bundle

The page shows the whole effect before anything happens — every message, where it is now,
where it would go. Operations are `move`, `add_flag`, `remove_flag`, `delete` and
`discard_container`; delete moves to Trash, and nothing expunges, because mail has no undo.

You can accept, reject with a reason, exclude a single item that does not belong and accept
the rest, or ask for a body to be fetched for a message whose subject is not enough to
decide on. An agent can withdraw its own bundle until somebody decides.

Accepting hands the work to a background runner: the queue gains a **Being applied**
section, the bundle page shows progress as it runs, and a failure — an unreachable
mailbox, most often — is shown where it happened with a retry button, instead of a
change that silently never lands. The same goes for **sync now** and body fetches; a
sync's progress streams to the page while it runs.

## Bundles about folders

A move can name a folder that does not exist yet. The page says so — *new folder, will be
created* — and nothing has been made while it sits in the queue: accepting the move is what
makes the folder, immediately before the first message goes into it. If it cannot be made,
nothing moves and the bundle is still there to accept again.

A `discard_container` bundle lists folders where the messages usually are, with what each
holds and what sits under it. Only empty ones are ever offered, because an empty folder is
the only thing here whose removal cannot lose mail — and each one is looked at again on the
server just before it goes, so a folder that has since had mail arrive in it is left alone
and says why. A whole branch can go in one bundle as long as the bundle holds every folder
in it; they are removed deepest first. INBOX and the special folders — Sent, Drafts, Trash,
Junk, Archive — are refused.

Homogeneity, not size, is what keeps a large bundle reviewable
([03](design/03-review.md) has the argument); the `[limits]` are there so a bundle can be
*rendered*, not so it can be understood.

## What "partial" means on a message

A sync reads header blocks, not messages, so what it can judge is limited: whether a
multipart message's parts are intact is a question about a body it has not fetched. It no
longer guesses — a message is `partial` when something is actually wrong with what was
read, and the reason is recorded beside it. Fetching the body settles the rest, and the
status, the attachments and the preview are all re-derived at that point.

A cache filled before this is corrected in place by a migration — `mailmindctl migrate`
runs it, and no re-downloading is needed. `mailmindctl sync --full` exists for the cases
that do: it re-reads every message rather than only what changed.

## When the mailbox has moved on

Each suggestion carries the premise it was proposed under. It is checked before the bundle
is shown and again per item immediately before applying — the second time is what matters,
because by then you have said yes.

A stale item is not applied to whatever is there instead. It says what changed, in words:
the message moved, its flags changed, the folder was recreated. Accepting a bundle that is
already visibly stale asks you to acknowledge that first. A bundle nobody decides expires
after `bundle_expiry_days`.

## When a bundle was built from a search

An agent can name a bundle's mail with a search rather than a list. When it has, the page
says so and shows the words it searched for.

The search ran once, when the bundle was proposed. What you see is the whole of it — nothing
is looked up again between now and accepting, and mail that arrives afterwards is not
quietly swept in. The list is what you are accepting; the search is only how it was found.

## When a bundle grew while you were reading it

The agent that proposed a bundle can add to it while it is still waiting. If that happens
after the page you are looking at was drawn, accepting is refused: you are told how many
arrived, and the bundle is shown again with them marked `added later`.

There is no way to acknowledge this and carry on, deliberately. Something dying can be
acknowledged, because the effect only got smaller. Something arriving cannot, because it
would mean approving mail nobody read. Read the page again and accept from that.

The day a bundle expires does not move when it grows.

## Accounts

The accounts page lists them, chooses the one you are working in, and syncs one on demand.

That choice is a **view, not a boundary**: it decides what the queue is about, and keeps
nothing from anybody, because the person at the keyboard owns all of it. A grant's account
scoping looks identical and *is* a boundary.

Adding an account belongs here — the row is the source of truth and the file is seed data —
but the form is not built. Today you add it to [the configuration](reference/configuration.md)
and run `mailmindctl account seed`.

Which means a seed can be wrong, and a wrong one used to be permanent: seed from a copy of
the example file once and `imap.example.org` is in this list forever.
`mailmindctl account list` shows what is there and which of it the configuration no longer
asks for; `mailmindctl account forget NAME` removes one, provided it holds no cached mail
and the file has stopped naming it.
