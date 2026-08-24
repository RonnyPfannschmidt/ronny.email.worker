# Would `aioimaplib` do instead of the synchronous client?

Recorded 2026-08-24. An investigation run with Claude; the measurements below were taken
against a real server and can be re-run, the reported-elsewhere items are marked as such.

**Answer: no, not against these versions.** Kept here because the reasons are specific and
dated, and because two of the three blockers are bugs rather than design — if they are
fixed the question is worth asking again.

---

## Which package

Two similar names, only one candidate.

- **`aioimap`** — a hobby wrapper *around* aioimaplib that calls a callback on new mail.
  Last release 0.2.7, February 2021; pulls in fastapi and uvicorn at runtime. Not a
  candidate.
- **`aioimaplib`** — the only maintained asyncio IMAP4rev1 client in the ecosystem, and
  what everything below is about. Version 2.0.1, moved from `bamthomas` to `iroco-co`.

## How it was tested

Against the same throwaway Dovecot the container tier uses
(`docker.io/antespi/docker-imap-devel`), seeded with `dev/seed_mailbox.py`, on CPython
3.13 and 3.14 — driving exactly the surface `mailmind.imap.client` needs.

Everything mailmind asks for is reachable on the wire: `ENABLE CONDSTORE`, `UID FETCH`
with `MODSEQ`, `(CHANGEDSINCE n)`, `UID MOVE`, `STATUS`, and LIST with `\Sent`/`\Trash`
attributes. So the question is not capability. It is what the library does with them.

## What it would fix

- `UID STORE (UNCHANGEDSINCE n) +FLAGS` goes through the **public** API. Dovecot's refusal
  comes back as `[MODIFIED 1] Conditional store failed`. `_conditional_store` would stop
  reaching into `_command_and_check` and `_imap.untagged_responses`. (imapclient 3.1.0,
  released 2026-01-17, still has no STORE modifiers.)
- IDLE is implemented properly, if push ever matters.

## What blocks it

### It falls over on an ordinary folder

`_handle_responses` recurses once per response line within a single TCP read. Same folder,
same server, `UID FETCH <range> (UID FLAGS MODSEQ)`:

| range | aioimaplib 2.0.1 | imapclient 3.1.0 |
|---|---|---|
| `1:1000` | OK 0.09s | OK 0.02s |
| `1:1500` | OK 0.20s | OK 0.03s |
| `1:3000` | **dead** (killed at 25s) | OK 0.05s |
| `1:5000` | **dead** (killed at 25s) | OK 0.08s |

The failure is `RecursionError` inside `data_received`, swallowed by the transport: the
command future never resolves, **the library's own `timeout=` never fires**, and the
connection is unusable afterwards — a following `NOOP` hangs too. Only an external
`asyncio.wait_for` bounds it. Upstream [issue #118], open, unfixed.

The threshold is TCP segmentation, not a message count, so a real network is worse than
these numbers. `sync_container` batches full syncs at `FETCH_BATCH = 200` and survives;
`fetch_changed_since` issues an unbounded `1:*` and does not — a first incremental sync
after a long gap walks straight into it.

### Read-only selection does not work

Only `select()` sets `state = SELECTED`; `examine()` does not. Every subsequent
`UID`/`FETCH`/`SEARCH` is then refused by the library's own guard:

```
Abort: command UID illegal in state AUTH
```

mailmind selects `readonly=True` by default, deliberately — so this is the default path,
and it needs `protocol.state` monkeypatched to work at all.

### GPL-3.0

mailmind is MPL-2.0. imapclient is BSD-3-Clause. A copyleft runtime dependency is a
decision, not a detail.

### Two more that cost quality rather than correctness

- **No response parser.** `Response.lines` is raw bytes with the `* ` stripped and literals
  as separate list entries; imapclient's `parse_fetch_response` rejects them outright
  (`bad response type: b'FETCH'`). FETCH parsing, `INTERNALDATE` → datetime and
  modified-UTF-7 folder names all become ours — LIST hands back `&AMQ-rchiv` for `Ärchiv`.
  Their line splitter is already known-wrong on unmatched parens in quoted strings
  ([issue #51]).
- **Connection errors are lost.** `IMAP4_SSL` against a plaintext port: the real
  `ssl.SSLError: WRONG_VERSION_NUMBER` lands in a fire-and-forget task ("Task exception was
  never retrieved") while the caller waits out the full timeout and is told nothing. Today
  that is `MailboxUnhealthy: cannot reach host:port: <reason>`, which is what
  [04](../04-mailbox-access.md) asks for. It would become "timed out".

## Maintenance, as of this date

Last release 2.0.1 in January 2025; last *code* commit April 2025 (a typehint fix) —
everything since is CI and docs. 45 open issues, several fatal and recent: fetch never
returns, `wait_server_push` never returning on `* EXISTS`, Python 3.14 build failures,
connection hangs immune to `wait_for`. Home Assistant is the large consumer and carries a
tail of IDLE-reconnect bugs traced here — *reported, not verified in this session.*
Roughly 645k downloads/month against imapclient's 1.35M; imapclient shipped 3.1.0 in
January 2026.

## What async would actually buy

The service is synchronous end to end on purpose: MCP tools are `def`, web routes are `def`
behind starlette's threadpool, `MailBackend` is a synchronous Protocol. Adopting
aioimaplib means making that protocol async and following it through sync, apply, service,
mcp, web and the fake — or running a loop per call, which discards the concurrency that was
the point. Both things async would buy are available without it: imapclient has
`idle()`/`idle_check()`/`idle_done()`, and per-folder parallelism is one connection per
thread, which blocking I/O serves fine.

## Where this leaves it

Stay on imapclient. Ask again if [issue #118] and the `examine()` state bug are fixed *and*
the licence question has an answer — and even then only if a workload turns up that threads
cannot serve. The cheaper way to get the conditional-store hack out of `client.py` is a PR
to imapclient adding `modifiers` to `_store`.

[issue #118]: https://github.com/iroco-co/aioimaplib/issues/118
[issue #51]: https://github.com/iroco-co/aioimaplib/issues/51
