"""Turning a message off the wire into something the rest of the service can hold.

Everything here is hostile input.  Nothing in a message decides what the service does; it
is parsed into data and marked as such.  Parsing that fails is recorded as having failed
rather than being silently treated as empty, because an empty body and an unreadable one
mean very different things to a reviewer.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import re
from email.headerregistry import Address
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

import attrs

PREVIEW_CHARS = 400


@attrs.frozen
class Link:
    text: str
    target: str


@attrs.frozen
class Attachment:
    filename: str | None
    content_type: str
    size: int


@attrs.frozen
class ParsedMessage:
    message_id: str | None
    subject: str | None
    date: Any
    from_address: str | None
    from_display: str | None
    addresses: tuple[tuple[str, str, str | None], ...]  # role, address, display name
    in_reply_to: str | None
    list_id: str | None
    has_list_unsubscribe: bool
    text_plain: str | None
    text_from_html: str | None
    links: tuple[Link, ...]
    attachments: tuple[Attachment, ...]
    #: "ok", "partial" or "unparseable".
    parse_status: str
    parse_detail: str | None
    #: The length of the blob that was parsed, which for a sync is the header block.
    #: What the message actually weighs comes from the server, not from here.
    size_bytes: int

    @property
    def preview(self) -> str | None:
        body = self.text_plain or self.text_from_html
        if not body:
            return None
        collapsed = re.sub(r"\s+", " ", body).strip()
        return collapsed[:PREVIEW_CHARS] or None

    @property
    def body_text(self) -> str:
        return "\n".join(filter(None, (self.text_plain, self.text_from_html)))

    def content_key(self) -> str:
        """A stable-enough identity when ``Message-ID`` is missing or duplicated."""
        material = "\x00".join(
            [
                self.message_id or "",
                self.subject or "",
                self.from_address or "",
                self.date.isoformat() if self.date else "",
                str(self.size_bytes),
            ]
        )
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


class _LinkExtractor(HTMLParser):
    """Pull (text, href) pairs and a text rendering out of HTML.

    The pairing matters more than the text: a link whose visible text disagrees with where
    it goes is a mechanical finding, and it cannot be found after the HTML is flattened.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.text_parts: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs_: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs_).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            self.links.append(Link(text="".join(self._link_text).strip(), target=self._href))
            self._href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.text_parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def _decode(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _addresses(message: EmailMessage, header: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    try:
        value = message.get(header)
        if value is None:
            return out
        for addr in getattr(value, "addresses", ()) or ():
            assert isinstance(addr, Address)
            if addr.addr_spec:
                out.append((addr.addr_spec.lower(), addr.display_name or None))
    except Exception:
        # A header that will not parse is not a reason to lose the rest of the message.
        for name, addr in email.utils.getaddresses([str(message.get(header, ""))]):
            if addr:
                out.append((addr.lower(), name or None))
    return out


def parse_message(raw: bytes) -> ParsedMessage:
    detail: list[str] = []
    status = "ok"
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same outcome here
        return ParsedMessage(
            message_id=None,
            subject=None,
            date=None,
            from_address=None,
            from_display=None,
            addresses=(),
            in_reply_to=None,
            list_id=None,
            has_list_unsubscribe=False,
            text_plain=None,
            text_from_html=None,
            links=(),
            attachments=(),
            parse_status="unparseable",
            parse_detail=f"{type(exc).__name__}: {exc}",
            size_bytes=len(raw),
        )

    if message.defects:
        status = "partial"
        detail.extend(type(d).__name__ for d in message.defects)

    roles = (("from", "From"), ("to", "To"), ("cc", "Cc"), ("reply_to", "Reply-To"))
    addresses: list[tuple[str, str, str | None]] = []
    for role, header in roles:
        for addr, display in _addresses(message, header):
            addresses.append((role, addr, display))

    from_entries = [a for a in addresses if a[0] == "from"]
    from_address = from_entries[0][1] if from_entries else None
    from_display = from_entries[0][2] if from_entries else None

    text_plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    try:
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            content_type = part.get_content_type()
            if disposition == "attachment" or (
                content_type not in ("text/plain", "text/html") and disposition != "inline"
            ):
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    Attachment(
                        filename=part.get_filename(),
                        content_type=content_type,
                        size=len(payload),
                    )
                )
                continue
            if content_type == "text/plain":
                text_plain_parts.append(_decode(part))
            elif content_type == "text/html":
                html_parts.append(_decode(part))
    except Exception as exc:  # noqa: BLE001
        status = "partial"
        detail.append(f"walk failed: {type(exc).__name__}: {exc}")

    links: list[Link] = []
    html_text_parts: list[str] = []
    for html in html_parts:
        extractor = _LinkExtractor()
        try:
            extractor.feed(html)
            extractor.close()
        except Exception as exc:  # noqa: BLE001
            status = "partial"
            detail.append(f"html: {type(exc).__name__}")
        links.extend(extractor.links)
        html_text_parts.append("".join(extractor.text_parts))

    text_plain = "\n".join(p for p in text_plain_parts if p).strip() or None
    text_from_html = "\n".join(p for p in html_text_parts if p).strip() or None

    for match in _URL_RE.finditer(text_plain or ""):
        links.append(Link(text=match.group(0), target=match.group(0)))

    date = None
    try:
        date = message.get("Date").datetime if message.get("Date") else None
    except Exception:  # noqa: BLE001
        status = "partial"
        detail.append("undecodable Date header")

    def header(name: str) -> str | None:
        try:
            value = message.get(name)
            return str(value) if value is not None else None
        except Exception:  # noqa: BLE001
            return None

    return ParsedMessage(
        message_id=header("Message-ID"),
        subject=header("Subject"),
        date=date,
        from_address=from_address,
        from_display=from_display,
        addresses=tuple(addresses),
        in_reply_to=header("In-Reply-To"),
        list_id=header("List-Id"),
        has_list_unsubscribe=message.get("List-Unsubscribe") is not None,
        text_plain=text_plain,
        text_from_html=text_from_html,
        links=tuple(links),
        attachments=tuple(attachments),
        parse_status=status,
        parse_detail="; ".join(detail) or None,
        size_bytes=len(raw),
    )
