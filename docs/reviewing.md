# Reviewing

The one step that cannot be skipped, and therefore the one that decides whether the whole
thing is worth using.

## Getting in

`mailmindctl serve` prints a link with the login key in it:

```
review UI  http://127.0.0.1:8765/?key=JhvKuPxtd_vlHWInseYw6GskTSzFHdxcMwe8kUAhpSc
           open that link once — it is the login, and nothing
           connecting over MCP is given it.
MCP        http://127.0.0.1:8765/mcp/
```

Open it once; the key becomes a session cookie and leaves the address bar, so it is not in
your browser history or in anything that logs a URL.

When the UI was brought up by `mailmindctl mcp --serve` instead, the link is *not* printed
— an MCP client collects that process's stderr into a log, and some put the log in front of
the model. It goes to a file with a mode on it, and stderr gets the path:

```
review UI  http://127.0.0.1:45911/  (this session only)
           the link that opens it is in /run/user/1000/mailmind/review-45911.link
           `mailmindctl review --open` follows it for you
```

`mailmindctl review` prints that link; `--open` follows it in a browser; `--port` picks
between several servers.

A restart mints a new key, so an open tab stops working and you go back to the terminal.

## The queue

The front page is what is waiting for you, for the account you are working in. Each entry
is a bundle: one operation over an enumerated list of messages, with the summary and the
reason its producer wrote.

`mailmindctl status` answers the same question without a browser.

## A bundle

The bundle page shows the whole effect before anything happens — every message, where it
is now, where it would go. Four operations exist: `move`, `add_flag`, `remove_flag` and
`delete`. Delete moves to the server's own Trash; nothing here expunges, because mail has
no undo.

What you can do:

- **Accept.** The service applies it, item by item, checking each premise again first.
- **Reject**, with a reason. The reason is recorded against the bundle.
- **Exclude an item**, if one message in the list does not belong with the rest, and accept
  what remains.
- **Load a body**, for a message where the subject line is not enough to decide.

An agent can also withdraw its own bundle before anybody has decided.

## When the mailbox has moved on

A suggestion carries the premise it was proposed under — where the message was, what state
it was in. That premise is checked twice: once before the bundle is shown to you, and again
per item immediately before it is applied. The second check is the one that matters, because
by then you have already said yes.

An item whose premise has gone stale is not applied to whatever is there instead. It says
what changed, in words you can act on — the message moved, its flags changed, the folder was
recreated — and you decide again. Accepting a bundle that is already visibly stale asks you
to acknowledge that first.

A bundle nobody decides expires after `bundle_expiry_days`, rather than accumulating.

## Why a two-hundred-message bundle is fine

The number was never the thing; homogeneity is. One operation and one target over an
enumerated list is reviewable at a size the same list would not be if each item could do
something different. A hundred messages moving to Archive is one decision shown a hundred
times; a hundred messages each doing their own thing is a hundred decisions dressed as one.

So `max_bundle_size` and `max_messages_per_request` guard against a bundle nobody can
*render*, not one nobody can *understand*, and they belong to the deployment rather than to
the design.

## Accounts

The accounts page lists what there is, lets you choose the one you are working in, and
syncs one on demand.

The choice belongs to you rather than to the tenant, and **it is a view, not a boundary**.
It decides what the queue is *about*; it keeps nothing from anybody, because the person at
the keyboard owns all of it — a link to a bundle in another account still opens. A grant's
account scoping on the agent surface looks identical and *is* a boundary: there, an account
outside the grant reads as absent.

Adding an account is a thing that belongs in this UI — the `account` row is the source of
truth and the configuration file is seed data for it — but the form is not built yet. Today
you add one to [the configuration](configuration.md) and run `mailmindctl bootstrap`.
