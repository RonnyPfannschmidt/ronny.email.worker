# How the review UI got a login

Recorded 2026-08-26. The three attempts at keeping an agent out of the review UI, moved
here from [12 — An agent of your own](../12-an-agent-of-your-own.md) when the current
state stopped needing its own history to be understood. Two of the three attempts were
not enough, and it is worth knowing why.

## First: nothing at all

The review UI had no login. Accepting was an ordinary form POST, and holding the URL the
model had just been handed was enough:

```
POST http://127.0.0.1:35607/bundle/1/accept  ->  200
  INBOX: 5 (was 6)    Archive: 1 (was 0)
```

No token, no browser, no form fields. One request, and mail moved.

## Second: it has to look like a person did it

Every route that changes something required the headers a browser sends when somebody
submits a form on a page it is showing — `sec-fetch-mode: navigate`, `sec-fetch-dest:
document`, `sec-fetch-site: same-origin`, and a matching `origin`. These are
[Fetch Metadata](https://developer.mozilla.org/en-US/docs/Glossary/Fetch_metadata_request_header)
headers, which scripts are forbidden to set, so inside a browser they cannot be forged.
A refusal writes a `ui_change_refused` event, because nothing changed but something tried,
and whoever owns the mail should be able to find that out.

The origin is checked alongside those headers, and that check spent a while refusing the
person it was written for: browsers derive a form POST's `Origin` from the referrer policy,
and this service was sending `Referrer-Policy: no-referrer`, so every button in the review
UI arrived as `Origin: null`. The policy is `same-origin` now, and a missing or opaque
origin is no longer held against a request whose fetch metadata already says same-origin —
a browser is allowed to withhold it, and the metadata is the part doing the work.

`Sec-Fetch-User: ?1` would be the better signal — "a person did this" rather than "a
document navigated" — and is deliberately not required, because Safari has never sent it
and a check that locks out a whole browser is a check somebody turns off.

This was never a boundary and was not meant as one. It changed the character of the act:
before, an agent reached the review UI by doing the obvious thing with an address it had
been given; after, by stating in four headers that a browser was showing a page to a
person. An accident became a lie. But a lie is one line of code:

```
POST with the four headers  ->  200, and the mail moves
```

## Third: a login, for local too

So there is one. Not a password — there is nobody here to have an account, and a
passphrase for a service on your own machine is friction protecting the wrong thing.
Instead a **key**, minted when the process starts and printed where the person is:

```
review UI  http://127.0.0.1:45911/  (this session only)
           the link that opens it is in /run/user/1000/mailmind/review-45911.link
           `mailmindctl review --open` follows it for you
```

Note what is *not* there. An MCP client collects the stderr of everything it spawns into a
log, and some put that log in front of the model — so the key goes to a file with a mode
on it, and stderr gets the path. `mailmindctl serve` prints the link outright, because that
is a command a person runs in their own terminal and its output is nobody's agent log.

Following the link once trades the key for an HttpOnly session cookie and drops it back out
of the address. The model is told `http://127.0.0.1:45911/` and nothing else. The same forged
request as above now gets a 401 that says, in as many words, that an agent reading it was
not given the key on purpose and should tell the person to look at their terminal.

The key is not hard to guess and does not need to be. Everything rests on it not being
told to the agent, which is a property of every channel that reaches one — the
instructions, tool results, resources, prompts, and the stderr an MCP client collects. The
test suite asserts all five.
