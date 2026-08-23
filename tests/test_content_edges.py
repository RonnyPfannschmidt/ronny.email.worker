"""Mail as it actually arrives, rather than as the RFCs describe it.

Everything here is a shape a real mailbox produces — bounces, spam, mailers that were
written in 1998 — and each one either broke something or is one line away from breaking it.
The fake and the container tiers prove the protocol; this proves the parser survives the
mail.
"""

from __future__ import annotations

import time

import pytest

from mailmind.content.findings import mechanical_findings
from mailmind.content.parse import parse_message

FROM = b"From: a@b.example\r\n"
CT = FROM + b"Content-Type: "


def message(headers: bytes, body: bytes = b"body\r\n") -> bytes:
    return headers + b"\r\n" + body


# --------------------------------------------------------------------- it comes back


@pytest.mark.parametrize(
    ("why", "raw"),
    [
        ("nothing at all", b""),
        ("a blank line", b"\r\n"),
        ("headers and no body", b"From: a@b.example\r\nSubject: x\r\n"),
        ("no From", message(b"To: me@example.org\r\nSubject: orphan")),
        ("two From headers", message(b"From: a@b.example\r\nFrom: c@d.example")),
        ("group syntax", message(b"From: undisclosed-recipients:;\r\nTo: f: a@b.example;")),
        ("no domain", message(b"From: root\r\nSubject: cron")),
        ("an unknown charset", message(b"From: =?x-nope?B?SGVsbG8=?= <a@b.example>")),
        ("base64 that is not", message(b"From: =?utf-8?B?not!base64?= <a@b.example>")),
        ("an unreadable date", message(b"From: a@b.example\r\nDate: Thu, 32 Jan 2026 99:99")),
        ("a header folded with tabs", message(b"From: a@b.example\r\nSubject: one\r\n\ttwo")),
        ("a Message-ID with no brackets", message(b"From: a@b.example\r\nMessage-ID: loose@x")),
        ("a charset that lies", message(CT + b"text/plain; charset=utf-8", b"\xe4\xf6\xfc")),
        ("a charset nobody has", message(CT + b"text/plain; charset=x-nope")),
        ("base64 mispadded", message(FROM + b"Content-Transfer-Encoding: base64", b"zzz")),
        ("html that never closes", message(CT + b"text/html", b"<div><p>hi<script>x()")),
        ("an anchor with no href", message(CT + b"text/html", b"<a>text</a>")),
    ],
)
def test_a_message_that_will_not_parse_still_comes_back(why, raw):
    """No shape of mail raises out of the parser: one bad message must not end a sync."""
    parsed = parse_message(raw)
    assert parsed.parse_status in {"ok", "partial", "unparseable"}, why
    for text in (parsed.subject, parsed.from_display, parsed.from_address, parsed.text_plain):
        if text is not None:
            text.encode("utf-8")  # storable, which is the whole of what the cache needs


# ------------------------------------------------------------------------- addresses


def test_the_null_return_path_is_not_somebody_s_address():
    """`<>` is what a bounce comes from. It used to be cached as the literal string, which
    then grouped in summarize_senders as though a person called `<>` had written."""
    parsed = parse_message(message(b"From: <>\r\nTo: real@example.net\r\nSubject: bounce"))
    assert parsed.from_address is None
    assert [a for _, a, _ in parsed.addresses] == ["real@example.net"]


def test_an_address_longer_than_an_address_can_be_is_left_out():
    """320 octets is the RFC's ceiling and the column's width. Anything past it is not an
    address, and storing it would only work until the database was not SQLite."""
    parsed = parse_message(message(b"From: " + b"a" * 400 + b"@example.net\r\nSubject: x"))
    assert parsed.from_address is None
    assert parsed.addresses == ()


# ------------------------------------------------------------------------------ NUL


@pytest.mark.parametrize(
    ("field", "raw"),
    [
        ("subject", message(b"From: a@b.example\r\nSubject: null\x00byte")),
        ("text_plain", message(b"From: a@b.example\r\nSubject: x", b"before\x00after")),
    ],
)
def test_a_nul_byte_does_not_reach_the_database(field, raw):
    """Spam carries them and truncated mail ends in them. SQLite keeps a NUL, Postgres
    refuses one outright, and everything in between disagrees about where a string ends."""
    value = getattr(parse_message(raw), field)
    assert value is not None and "\x00" not in value


# ------------------------------------------------------------------------------ time


def test_a_date_with_no_timezone_is_read_as_utc_rather_than_dropped():
    """Plenty of mailers omit it. Nothing downstream can compare a naive datetime with an
    aware one, so this is the point where that has to be decided."""
    parsed = parse_message(message(b"From: a@b.example\r\nDate: Mon, 17 Aug 2026 09:00:00"))
    assert parsed.date is not None
    assert parsed.date.replace(tzinfo=None).isoformat() == "2026-08-17T09:00:00"


# ------------------------------------------------------------------------ big things


def test_a_long_run_of_word_characters_does_not_hang_the_parser():
    """The address hunt in the body used to be quadratic: every position started a scan to
    the end of the text. A 900 KB body of one token took hours, in whichever thread had
    asked for it — a review UI request, or a sync. It is bounded now, the way an address is.
    """
    raw = message(b"From: a@b.example\r\nSubject: x", b"x" * 900_000)
    parsed = parse_message(raw)
    started = time.monotonic()
    mechanical_findings(parsed)
    assert time.monotonic() - started < 5, "the address scan is quadratic again"


def test_an_address_in_a_body_stops_at_the_end_of_the_sentence():
    """A side effect of bounding it, and an improvement: the full stop that ended the
    sentence used to be part of the address, which then matched no header address and was
    reported as one the body had introduced."""
    raw = message(b"From: a@b.example\r\nSubject: x", b"write to me@example.net.")
    parsed = parse_message(raw)
    codes = {f.code: f for f in mechanical_findings(parsed)}
    assert codes["body_only_address"].evidence["addresses"] == ["me@example.net"]
