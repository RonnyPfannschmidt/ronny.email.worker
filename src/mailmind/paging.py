"""One shape for every bounded answer, and one way to ask for the rest of it.

05 asks that a request matching more than the limit gets less *and is told so*.  That
promise was kept in rows, which is not what a client truncates on.  opencode cuts a tool
result at around 50 KB, and the envelope used to sit *after* the rows — so the cut took
away precisely the fields saying the answer was incomplete, and left JSON that no longer
parsed.  A listing of 50 messages measured 41 KB, a folder list 66 KB and a bundle of 400
items 208 KB, none of which the row limit noticed.

So: the envelope leads, the limit is in bytes as well as rows, and what was left out is
reachable.  An answer with more behind it carries a ``next_cursor``, and handing that back
continues where the last one stopped.

A cursor is a position in *a response*, not in a list.  A message detail holds several
lists and a body, and one cursor advances whichever of them were cut — otherwise a caller
would juggle five of them to read one message.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pydantic_core


def weigh(value: Any) -> int:
    """The bytes this becomes on the wire.

    Serialised the way the MCP SDK serialises a tool result — ``to_json`` at ``indent=2``
    — because measuring it any other way is measuring something nobody sends.  The
    indentation is not a rounding error: it is most of the difference between a listing
    that fits and one that does not.
    """
    return len(pydantic_core.to_json(value, fallback=str, indent=2))


#: Rows are weighed one at a time and summed, which undercounts by the extra indentation
#: each row picks up from sitting inside the envelope.  The overcount is proportional and
#: small; this covers it.
_SLACK = 1.15

#: Held back for the envelope's own keys before any row is measured.
_ENVELOPE_BYTES = 512

# A budget is a target, not a ceiling.  What is weighed is the rows and the body; the
# scalar fields around them — a subject, a sender, the keys of each section — are not,
# so a fitted answer lands a little over.  Measured overshoot is under ten percent, which
# is why the configured default sits well below what a client will take rather than just
# under it.


class CursorError(ValueError):
    """A cursor that cannot be honoured, which is never silently ignored.

    Continuing from a cursor that belongs to a different request would answer with a page
    of some other question — so a mismatch refuses rather than starting over from the
    top, which would look like progress and be a loop.
    """


def fingerprint(*parts: Any) -> str:
    """A short, stable digest of what was asked, so a cursor cannot cross requests."""
    material = "\x00".join("\x01" if p is None else str(p) for p in parts)
    return hashlib.blake2s(material.encode(), digest_size=6).hexdigest()


def encode(kind: str, guard: str, at: dict[str, Any]) -> str:
    """Freeze a position into something a caller passes back and does not read.

    Opaque on purpose: a caller that takes a cursor apart is a caller depending on how
    this pages, and every one of these surfaces pages differently underneath — keyset
    here, an offset into a rank ordering there.
    """
    blob = json.dumps({"v": 1, "k": kind, "g": guard, "at": at}, separators=(",", ":"))
    return base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def decode(raw: str | None, *, kind: str, guard: str) -> dict[str, Any]:
    """The position a cursor carries, or an empty one when there is no cursor."""
    if not raw:
        return {}
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception as exc:
        raise CursorError("this cursor is not one of ours") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise CursorError("this cursor is not one of ours")
    if payload.get("k") != kind:
        raise CursorError(f"this cursor is for {payload.get('k')!r}, not {kind!r}")
    if payload.get("g") != guard:
        raise CursorError(
            "this cursor belongs to a different request — the filter, the folder or the "
            "query has changed since it was issued. Start again without a cursor."
        )
    at = payload.get("at")
    return at if isinstance(at, dict) else {}


class Budget:
    """Bytes left in one response, spent across however many lists it holds.

    Shared rather than per-list so that a message with a long body and three hundred links
    comes back inside one limit instead of three.
    """

    def __init__(self, limit_bytes: int | None) -> None:
        self.remaining = None if limit_bytes is None else max(limit_bytes - _ENVELOPE_BYTES, 0)

    def fit(self, rows: list[dict]) -> tuple[list[dict], bool]:
        """The rows that fit, and whether any were dropped.

        The first row is always kept even when it alone overruns.  A page that can be
        empty while more is waiting is a cursor that never advances, and a caller
        following it politely would loop forever.
        """
        if self.remaining is None or not rows:
            return rows, False
        kept: list[dict] = []
        for row in rows:
            cost = int(weigh(row) * _SLACK)
            if kept and cost > self.remaining:
                return kept, True
            kept.append(row)
            self.remaining = max(self.remaining - cost, 0)
        return kept, False

    def take_text(self, text: str, start: int) -> tuple[str, bool]:
        """As much of a blob as is left to spend, from ``start``."""
        if self.remaining is None:
            return text[start:], False
        room = max(self.remaining, 1024)
        chunk = text[start : start + room]
        self.remaining = max(self.remaining - len(chunk), 0)
        return chunk, start + len(chunk) < len(text)


def envelope(
    rows: list[dict],
    total: int,
    *,
    kind: str,
    next_cursor: str | None = None,
    steer: str | None = None,
) -> dict:
    """The one shape every bounded observation comes back in.

    Order matters here and is not cosmetic: a client that cuts the response mid-way should
    lose rows, never the four fields that say rows are missing.
    """
    more = next_cursor is not None
    note = None
    if more:
        note = f"{total} {kind} match; {len(rows)} returned. Pass next_cursor to continue" + (
            f", {steer}." if steer else "."
        )
    elif total > len(rows):
        # Reached only where a surface cannot issue a cursor for what it left out.
        note = f"{total} {kind} match; {len(rows)} returned."
    return {
        "returned": len(rows),
        "total_matching": total,
        "truncated": more,
        "next_cursor": next_cursor,
        "note": note,
        kind: rows,
    }


def section(rows: list[dict], total: int, *, kind: str, through: int) -> dict:
    """One list inside a response that holds several of them.

    Same promise as :func:`envelope` and deliberately no cursor of its own: a message
    detail carries recipients, links, attachments, findings and a body, and five cursors
    to read one message is a surface nobody would use.  The response carries one, and it
    advances whichever parts were cut.

    ``through`` is how far into the whole list this response has now reached, counting the
    pages before it — which is why ``truncated`` is not ``total > len(rows)``.  On a second
    page a finished section returns no rows, and that comparison called it truncated.
    """
    return {
        "offset": through - len(rows),
        "returned": len(rows),
        "total_matching": total,
        "truncated": through < total,
        kind: rows,
    }


def page(
    rows: list[dict],
    total: int,
    *,
    kind: str,
    budget: Budget,
    guard: str,
    positions: list[dict],
    fetched_more: bool = False,
    steer: str | None = None,
    cursor_kind: str | None = None,
    end_position: dict | None = None,
) -> dict:
    """Fit rows to the budget and issue the cursor for whatever did not fit.

    ``positions`` runs alongside ``rows``: where the next page starts if this row is the
    last one shown.  Kept beside the rows rather than read back out of them because what a
    keyset resumes from — a placement id, a rank offset — is not always something the row
    is meant to carry.

    ``fetched_more`` is what the query learnt by asking for one row more than the limit.
    The cursor comes from the last row that *survived the budget*, never from the last one
    fetched: take it from the wrong one and every trimmed page silently skips whatever the
    budget dropped.

    ``end_position`` is where a page with no rows left in it resumes.  A search can fetch a
    full window and answer with none of it, every hit having been mail that has since left
    the mailbox; without this the answer would say there is no more to come and lose the
    rest of the results.

    ``cursor_kind`` separates what the rows are called from what the cursor belongs to,
    which a search needs: its rows are messages, its cursor is not a message listing's.
    """
    assert len(positions) == len(rows), "a position per row, or the cursor lies"
    kept, dropped = budget.fit(rows)
    more = dropped or (fetched_more and len(kept) == len(rows))
    at = positions[len(kept) - 1] if kept else end_position
    cursor = encode(cursor_kind or kind, guard, at) if more and at is not None else None
    return envelope(kept, total, kind=kind, next_cursor=cursor, steer=steer)
