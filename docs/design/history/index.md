# Design history

Starting-point documents. These are **not** specifications and **not** decisions.

They are exploratory design work — mostly transcripts and artifacts from sessions with
Claude — kept verbatim so the origin of an idea stays traceable once it has been folded
into a real spec. They are recorded as history, not as direction.

The current design lives in [`docs/design/`](../index.md). Where a document here disagrees with it, the
current design wins. Where it disagrees with reality, both lose and the design gets fixed.

Documents are dated by when they were recorded and are never edited afterwards. Folding one
into the current design is done incrementally, a component at a time; the source document
stays here unchanged.

## Documents

| Date | Document | Status |
|---|---|---|
| 2026-08-12 | [mailmind — Testability & CI Plan](2026-08-12-mailmind-testability-and-ci-plan.md) | not yet folded into specs |

### 2026-08-12 — mailmind — Testability & CI Plan

Written as a replacement for §14 of a larger "build prompt" that is not recorded here, so
it references material it does not define: invariants I1–I7, the §7 capacity limits, the
connection state machine (`READY → DEGRADED → QUARANTINED → …`), the idea lifecycle, the
MCP tool surface, the single-threaded core, and a `mailmindctl` CLI. Treat those
references as open questions, not as settled design.
