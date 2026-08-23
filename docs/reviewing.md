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
where it would go. Operations are `move`, `add_flag`, `remove_flag` and `delete`; delete
moves to Trash, and nothing expunges, because mail has no undo.

You can accept, reject with a reason, exclude a single item that does not belong and accept
the rest, or load a body for a message whose subject is not enough to decide on. An agent
can withdraw its own bundle until somebody decides.

Size is not what makes a bundle unreviewable — homogeneity is. A hundred messages moving to
Archive is one decision shown a hundred times; a hundred messages each doing their own thing
is a hundred decisions dressed as one. The `[limits]` are there so a bundle can be
*rendered*, not so it can be understood.

## When the mailbox has moved on

Each suggestion carries the premise it was proposed under. It is checked before the bundle
is shown and again per item immediately before applying — the second time is what matters,
because by then you have said yes.

A stale item is not applied to whatever is there instead. It says what changed, in words:
the message moved, its flags changed, the folder was recreated. Accepting a bundle that is
already visibly stale asks you to acknowledge that first. A bundle nobody decides expires
after `bundle_expiry_days`.

## Accounts

The accounts page lists them, chooses the one you are working in, and syncs one on demand.

That choice is a **view, not a boundary**: it decides what the queue is about, and keeps
nothing from anybody, because the person at the keyboard owns all of it. A grant's account
scoping looks identical and *is* a boundary.

Adding an account belongs here — the row is the source of truth and the file is seed data —
but the form is not built. Today you add it to [the configuration](reference/configuration.md)
and run `mailmindctl bootstrap`.

Which means a seed can be wrong, and a wrong one used to be permanent: bootstrap a copy of
the example file once and `imap.example.org` is in this list forever.
`mailmindctl account list` shows what is there and which of it the configuration no longer
asks for; `mailmindctl account forget NAME` removes one, provided it holds no cached mail
and the file has stopped naming it.
