# `mailmind` — Testability & CI Plan
 
Replaces §14 of the build prompt. Written for a suite that must run against several real
mail servers plus in-process fakes, in CI, without going flaky.
 
---
 
## 1. The three target tiers
 
Every test runs against a **target**: a provisioned mailbox plus a declared capability set.
 
| Tier | Targets | When | Blocking |
|---|---|---|---|
| `fake` | in-process IMAP fake, in-process Gmail API fake | every commit | yes |
| `container` | Dovecot (pinned to Uberspace's version), Dovecot (latest), Stalwart, GreenMail | every PR | yes |
| `live` | real Uberspace test mailbox, throwaway Google account | nightly + manual | no (alerts) |
 
Default invocation is `-m "not live"`. The `live` tier needs secrets, so it must skip
cleanly — not error — when they're absent (forks, first-time contributors).
 
Why Dovecot twice: pinning to Uberspace's current version is what the service actually runs
against; tracking latest is how you find out about a Dovecot upgrade *before* Uberspace does
it to you. Stalwart is there purely to catch accidental Dovecot-specific assumptions.
 
---
 
## 2. Capabilities gate tests — and absence must fail, not skip
 
The obvious design is `@pytest.mark.skipif(not has_condstore)`. **Do not do that**, because
a Dovecot config regression that drops CONDSTORE turns the entire conditional-store suite
green by skipping, which is worse than red.
 
Instead, two separate things:
 
1. **A committed expected-capability matrix**, per target, in `targets.toml`:
```toml
   [targets.dovecot-uberspace]
   kind = "generic_imap"
   caps = ["CONDSTORE", "QRESYNC", "MOVE", "SPECIAL-USE", "UIDPLUS", "IDLE"]
 
   [targets.gmail-api]
   kind = "gmail_api"
   caps = ["INCREMENTAL_HISTORY", "BATCH_MODIFY", "STABLE_MESSAGE_ID"]
   # deliberately absent: CONDITIONAL_STORE
```
 
2. **A probe run as a session fixture** that queries the live target's actual capabilities
   and **diffs them against the declared matrix**. Any divergence — missing *or* unexpectedly
   present — fails the session immediately, before any test runs.
Only then do tests skip, and only on *declared* absence:
 
```python
@pytest.mark.requires_caps("CONDITIONAL_STORE")
def test_apply_rejects_when_message_changed_since(target, ...): ...
```
 
`pytest_collection_modifyitems` consults the declared matrix (not the probe) to deselect.
The probe's job is to prove the declaration is still true; the declaration's job is to decide
what runs. Keeping those separate is what makes a skipped test trustworthy.
 
Include the target name in the test id so failures are legible:
`test_apply_rejects_when_changed[dovecot-uberspace]`.
 
---
 
## 3. Target fixture contract
 
Every target — fake, container, or live — implements the same provisioning protocol. If a
target can't implement one of these, that's a capability, declared and gated, not a
special case in the test body.
 
```python
class MailTarget(Protocol):
    caps: frozenset[str]
    kind: Literal["generic_imap", "gmail_imap", "gmail_api"]
 
    def seed(self, corpus: Corpus) -> SeedMap: ...
        # returns logical-name -> target-specific message key
 
    def out_of_band_mutate(self, ops: list[ExternalOp]) -> None: ...
        # simulate the operator or another client: flag, move, relabel, delete
 
    def force_uidvalidity_change(self, folder: str) -> None: ...
    def force_auth_failure(self) -> None: ...
    def force_history_gap(self) -> None: ...       # gmail: expire historyId
    def advance_clock(self, delta: timedelta) -> None: ...  # fake targets only
```
 
`out_of_band_mutate` is the most important method in the suite. The interesting behaviour of
this service is entirely about **external change racing internal state** — a message moved
between proposal and apply, an operator who already filed the mail, a `UIDVALIDITY` bump.
None of that is testable without a way to change the mailbox behind the service's back.
 
Implementations:
 
- Dovecot container: a second IMAP connection, or `doveadm`. `force_uidvalidity_change` =
  `doveadm mailbox delete` + `create` (or rewrite `dovecot-uidlist`).
- Gmail live: a second API client with its own credential.
- Fakes: direct method calls.
- GreenMail: second connection; `force_uidvalidity_change` unsupported → declare the
  capability absent.
---
 
## 4. Never assert on target-specific identifiers
 
UIDs, Gmail message ids, `UIDVALIDITY` values and internal dates all differ per target and
per run. Tests address messages by **logical corpus name** and translate through `SeedMap`:
 
```python
key = seed["msg_with_body_only_address"]
```
 
Any test containing a literal UID or message id is a bug. Add a lint check for it — it's the
single most common source of "passes on Dovecot, fails on Stalwart".
 
---
 
## 5. Determinism: drive the core, don't sleep
 
The single-threaded core is a testing asset. Expose a deterministic drive mode:
 
```python
core.submit(event)          # enqueue without processing
core.step()                 # process exactly one event, return the new seq
core.drain()                # process until the queue is empty
```
 
Tests assert on the event log and the fold, never on wall-clock timing. **No `sleep()`
anywhere in the suite.** The ordering tests that actually matter become exact rather than
flaky:
 
```python
def test_fetcher_result_computed_before_acceptance_is_revalidated(core, target):
    fetch = fetcher_result_at(seq=core.seq)
    core.submit(acceptance_event())      # lands first
    core.submit(fetch)                   # computed against a now-stale seq
    core.drain()
    assert fetch_outcome(core) == "REVALIDATION_REJECTED"
```
 
Same for TTL: inject the clock into the core and advance it explicitly. Never wait for a real
30-minute TTL, and never shorten TTLs in test config to make waiting tolerable — that tests a
different configuration than the one you ship.
 
For staleness driven by the *mailbox* rather than the clock, drive the event
(`out_of_band_mutate`), not the time.
 
---
 
## 6. Fakes must be verified, not trusted
 
A fake that drifts from the real API is worse than no fake, because it makes CI confidently
wrong. Two mechanisms:
 
**Same suite, both targets.** The conformance suite is the definition of correct behaviour.
The fake earns trust only by passing the identical tests the live target passes. Any test
that runs against `gmail-api-live` must also run against `gmail-api-fake` unless a declared
capability difference excludes it.
 
**Record and replay.** The nightly live run records real request/response pairs. A separate
job replays them against the fake and diffs. Drift shows up as a failing replay, with the
recorded interaction as the evidence. Store cassettes in-repo with secrets scrubbed at
capture time, not at commit time.
 
The Gmail fake must reproduce the behaviours that will bite in production, not just the happy
path:
 
- `historyId` too old → 404 → forces the full-resync path.
- User label ids opaque and distinct from label names; a rename that leaves the id stable.
- `batchModify` partial application semantics.
- 429 with quota-unit accounting and `Retry-After`.
- `format=metadata` vs `full` returning genuinely different payloads.
---
 
## 7. Adversarial corpus
 
The seed corpus is a fixture package, versioned, shared across all targets. Beyond ordinary
mail it MUST contain:
 
- A message whose body contains an email address that is **not** in any header (drives I7).
- Unicode Tag characters (U+E0000–U+E007F) in body and in a header.
- Reference-style Markdown links and a remote image reference.
- A message with no `Message-ID`.
- Two messages sharing a `Message-ID` in different folders.
- 8-bit and RFC 2047-encoded headers, including a subject that decodes to something
  instruction-shaped.
- A malformed MIME structure and a truncated multipart boundary.
- A folder large enough to exceed the §7 message-window cap.
- A message whose `From` display name spoofs another participant's address.
These double as security regression tests. Each one gets a test asserting the *specific*
mitigation, not just "doesn't crash".
 
---
 
## 8. What must never be faked
 
The authorization and acceptance boundary. I1, I3, I5 tests run against the **real** storage
layer with the **real** role separation — a mocked DB proves nothing about whether the MCP
request path can write an acceptance record. Run them against a real SQLite (or whatever
ships) with the production role configuration.
 
Likewise the ingress rule that rejects `Mcp-Name: mail_apply*` on the agent route: test it
against a real ingress in a kind cluster, not by unit-testing the config parser.
 
---
 
## 9. CI layout
 
```yaml
jobs:
  fast:            # fake targets, full suite, every push
  containers:      # matrix over dovecot-pinned, dovecot-latest, stalwart, greenmail
  live:            # nightly + workflow_dispatch; needs secrets; continue-on-error + alert
  replay:          # nightly; replays recorded cassettes against fakes
  capability-drift: # daily; probe only, no tests
```
 
**`capability-drift` is separate from the test suite on purpose.** It runs `mailmindctl
probe` against Uberspace and Gmail, diffs against `targets.toml`, and fails loudly. It is the
early warning for "Uberspace upgraded Dovecot" and "Google changed a default", and it must
not be buried inside a test job whose failure looks like a code regression.
 
Container targets come up via docker compose with pinned image digests, not tags. A target
that fails to become ready within its timeout fails the job — it must never silently degrade
to skipping.
 
---
 
## 10. Coverage that matters
 
Track these explicitly; line coverage is not the useful signal here:
 
- Every invariant I1–I7 has at least one test per applicable target.
- Every connection state transition (`READY → DEGRADED → QUARANTINED → …`) has a test that
  reaches it by causing the real condition, not by setting the state directly.
- Every capacity limit in §7 of the build prompt has a saturation test asserting the specific
  error code and that the review UI stays responsive.
- Every idea terminal state is reached by at least one test.
- Every `precondition: CONDITIONAL` op has a test proving it fails when the message changed,
  on a target that declares `CONDITIONAL_STORE`.
- Every `precondition: BEST_EFFORT` op has a test proving the service *reports* the weaker
  guarantee rather than silently succeeding.