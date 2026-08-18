"""Mechanical findings.

Decidable without a model, and therefore not something an agent can talk its way around.
02 puts the line here deliberately: an assessing app can be talked into a wrong reading,
but it must not be able to report a valid signature.  These are the cheap end of that —
parsing facts, computed by the service, that a producer cannot overwrite.

Where exactly the line falls is an experiment.  This is the minimum worth having; each
addition costs implementation and is a place to be subtly wrong.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from urllib.parse import urlsplit

import attrs

from mailmind.content.parse import ParsedMessage


@attrs.frozen
class MechanicalFinding:
    code: str
    detail: str
    evidence: dict[str, object] = attrs.field(factory=dict)


#: Characters that are present but do not render.  Tag characters are the interesting
#: ones: they can carry a whole second instruction-shaped message invisibly.
_INVISIBLE_RANGES = (
    (0xE0000, 0xE007F),  # Unicode Tag characters
    (0x200B, 0x200F),  # zero width space/joiners, LTR/RTL marks
    (0x202A, 0x202E),  # bidi embedding and override
    (0x2066, 0x2069),  # bidi isolates
)

_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _invisible_chars(text: str) -> list[str]:
    found = []
    for char in text:
        point = ord(char)
        if any(low <= point <= high for low, high in _INVISIBLE_RANGES):
            found.append(f"U+{point:04X} {unicodedata.name(char, 'unnamed')}")
    return found


def _host(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def _registrable(host: str) -> str:
    """Last two labels.  Crude, and deliberately so — no public-suffix list is bundled.

    It over-reports on ``co.uk``-style suffixes, which is the safe direction for a
    finding whose job is to make a reviewer look.
    """
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def mechanical_findings(
    parsed: ParsedMessage,
    *,
    is_known_sender: Callable[[str], bool] | None = None,
) -> list[MechanicalFinding]:
    findings: list[MechanicalFinding] = []

    if parsed.parse_status == "unparseable":
        findings.append(
            MechanicalFinding(
                "unparseable",
                "The message could not be parsed. It is not empty; it is unreadable.",
                {"detail": parsed.parse_detail},
            )
        )
        return findings
    if parsed.parse_status == "partial":
        findings.append(
            MechanicalFinding(
                "malformed_mime",
                "The message parsed with defects; parts of it may be missing.",
                {"detail": parsed.parse_detail},
            )
        )

    # A display name that names an address is claiming to be somebody.  Identity comes
    # from the parsed address; the display name is decoration.
    if parsed.from_display and parsed.from_address:
        claimed = _ADDRESS_RE.findall(parsed.from_display)
        actual_domain = parsed.from_address.rpartition("@")[2]
        for candidate in claimed:
            if candidate.lower() != parsed.from_address:
                findings.append(
                    MechanicalFinding(
                        "display_name_spoofs_address",
                        f"The display name names {candidate}, but the message is from "
                        f"{parsed.from_address}.",
                        {
                            "display_name": parsed.from_display,
                            "claimed": candidate,
                            "actual": parsed.from_address,
                            "actual_domain": actual_domain,
                        },
                    )
                )

    for where, text in (
        ("subject", parsed.subject or ""),
        ("display_name", parsed.from_display or ""),
        ("body", parsed.body_text),
    ):
        invisible = _invisible_chars(text)
        if invisible:
            findings.append(
                MechanicalFinding(
                    "invisible_characters",
                    f"The {where} contains {len(invisible)} character(s) that are present "
                    f"but do not render.",
                    {"where": where, "characters": sorted(set(invisible))[:20]},
                )
            )

    for link in parsed.links:
        target_host = _host(link.target)
        if not target_host:
            continue
        text_host = _host(link.text) or (
            link.text.strip().lower()
            if re.fullmatch(r"[\w-]+(\.[\w-]+)+", link.text.strip())
            else None
        )
        if text_host and _registrable(text_host) != _registrable(target_host):
            findings.append(
                MechanicalFinding(
                    "link_target_mismatch",
                    f"A link reading {text_host} goes to {target_host}.",
                    {"text": link.text, "target": link.target},
                )
            )

    if not parsed.message_id:
        findings.append(
            MechanicalFinding(
                "no_message_id",
                "The message has no Message-ID, so it cannot be matched across folders.",
            )
        )

    header_addresses = {addr for _, addr, _ in parsed.addresses}
    body_addresses = {a.lower() for a in _ADDRESS_RE.findall(parsed.body_text)}
    body_only = sorted(body_addresses - header_addresses)
    if body_only:
        findings.append(
            MechanicalFinding(
                "body_only_address",
                "The body names addresses that appear in no header.",
                {"addresses": body_only[:10]},
            )
        )

    if is_known_sender is not None and parsed.from_address:
        if not is_known_sender(parsed.from_address):
            findings.append(
                MechanicalFinding(
                    "first_contact",
                    f"No earlier message from {parsed.from_address} is cached.",
                    {"address": parsed.from_address},
                )
            )

    return findings
