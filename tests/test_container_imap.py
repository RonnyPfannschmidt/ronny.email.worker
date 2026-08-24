"""The real client against a real IMAP server.

The fake proves the logic; this proves the wire.  A fake that has drifted from the thing
it stands for is worse than no fake, because it makes the suite confidently wrong — so the
same behaviours are asserted here against a server that actually speaks IMAP.

Skipped unless ``MAILMIND_IMAP_TARGET`` names one, so a checkout with no container runtime
still runs the rest of the suite.  It skips on absence of a *target*, never on absence of
a capability: a server that quietly stopped offering CONDSTORE must turn this red.
"""

from __future__ import annotations

import email
import os

import pytest

from mailmind.config import AccountConfig, Login
from mailmind.imap.backend import MailBackend
from mailmind.imap.sync import flags_hash
from tests.corpus import CORPUS

TARGET = os.environ.get("MAILMIND_IMAP_TARGET")

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(not TARGET, reason="MAILMIND_IMAP_TARGET is not set"),
]


@pytest.fixture
def target() -> AccountConfig:
    host, _, port = TARGET.partition(":")
    os.environ.setdefault("MAILMIND_TEST_PASSWORD", "secret")
    return AccountConfig(
        name="container",
        host=host,
        port=int(port or 143),
        use_ssl=False,
        login=Login(
            username=os.environ.get("MAILMIND_IMAP_USER", "me@example.org"),
            password="env://MAILMIND_TEST_PASSWORD",
        ),
    )


@pytest.fixture
def out_of_band(target):
    """A second connection, the way another mail client would be.

    Test setup runs through this rather than through the code under test, and it is also
    how a message gets touched behind mailmind's back.
    """
    from imapclient import IMAPClient

    client = IMAPClient(target.host, port=target.port, ssl=target.use_ssl)
    client.login(target.login.username, target.login.resolve())
    yield client
    client.logout()


@pytest.fixture
def clean_mailbox(out_of_band):
    """Start from a mailbox whose contents this test knows.

    Some targets give every test its own user and some have exactly one, so the suite
    empties rather than assuming either.
    """
    for folder in ("Archive", "INBOX"):
        if not out_of_band.folder_exists(folder):
            continue
        out_of_band.select_folder(folder, readonly=False)
        uids = out_of_band.search(["ALL"])
        if uids:
            out_of_band.delete_messages(uids)
            out_of_band.expunge()
    # Folders are emptied, not deleted: GreenMail drops the connection on DELETE, and a
    # target-specific cleanup would be a target-specific test.
    return out_of_band


def ensure_folder(client, name: str) -> None:
    if not client.folder_exists(name):
        client.create_folder(name)


@pytest.fixture
def backend(target, clean_mailbox):
    from mailmind.imap.client import ImapBackend

    backend = ImapBackend(target)
    yield backend
    backend.close()


@pytest.fixture
def seeded(backend, clean_mailbox):
    """Put the corpus in INBOX and return a map of logical name to UID.

    Seeding goes through APPEND rather than SMTP so the suite needs an IMAP server and
    nothing else.  Nothing in these tests names a UID directly; they differ per server and
    per run, and a test that hardcodes one passes on Dovecot and fails on Stalwart for
    reasons that have nothing to do with the code.
    """
    for raw in CORPUS.values():
        clean_mailbox.append("INBOX", raw)

    info = backend.select("INBOX", readonly=True)
    assert info.message_count == len(CORPUS), "seeding did not land"

    by_subject = {}
    for envelope in backend.fetch_envelopes("INBOX"):
        parsed = email.message_from_bytes(envelope.headers or b"")
        by_subject[str(parsed.get("Subject", ""))] = envelope.uid
    return {
        name: by_subject[str(email.message_from_bytes(raw).get("Subject", ""))]
        for name, raw in CORPUS.items()
    }


def test_the_backend_protocol_is_satisfied_by_the_real_client(backend):
    assert isinstance(backend, MailBackend)


def test_capabilities_are_reported_and_not_guessed(backend):
    caps = backend.capabilities()
    assert "IMAP4REV1" in caps or "IMAP4REV1".lower() in {c.lower() for c in caps}


def test_special_use_arrives_normalised_from_a_real_server(backend, out_of_band):
    """Whatever case the server chose, one spelling leaves the client.

    The applier looks up a trash folder by that spelling, so a server announcing
    ``\\Trash`` and a lookup asking for ``trash`` is a delete that can never find
    anywhere to go.
    """
    from mailmind.imap.backend import SPECIAL_USE

    ensure_folder(out_of_band, "Trash")
    reported = {c.name: c.special_use for c in backend.list_containers()}
    assert reported, "the server listed no folders at all"
    for name, special_use in reported.items():
        assert special_use is None or special_use in SPECIAL_USE, (
            f"{name} reported {special_use!r}, which nothing downstream looks for"
        )


#: A German mailbox has these, and IMAP does not carry them as UTF-8: names travel in
#: modified UTF-7, where `&` is the shift character and so has to be escaped as `&-`. A
#: folder called `Ärger & Co` exercises both halves of that in one name.
AWKWARD_FOLDERS = ("Gelöschte Objekte", "Entwürfe", "Ärger & Co", "with space")


def test_folder_names_survive_the_wire_in_the_alphabet_they_were_written_in(
    backend, out_of_band
):
    """187 folders on a real account, and the interesting ones are not called Archive."""
    for name in AWKWARD_FOLDERS:
        ensure_folder(out_of_band, name)

    found = {c.name: c for c in backend.list_containers()}
    for name in AWKWARD_FOLDERS:
        assert name in found, f"{name!r} did not come back as it went in"
        assert backend.select(name, readonly=True) is not None, f"{name!r} cannot be opened"


def test_a_message_in_a_folder_with_an_umlaut_is_fetched_like_any_other(
    backend, out_of_band
):
    """Selecting it is one thing; the FETCH that follows names it again."""
    ensure_folder(out_of_band, "Ärger & Co")
    out_of_band.append(
        "Ärger & Co",
        b"From: Kunde <kunde@example.net>\r\nSubject: Beschwerde\r\n"
        b"Message-ID: <umlaut-folder@example.net>\r\n"
        b"Date: Mon, 17 Aug 2026 09:00:00 +0000\r\n\r\nText\r\n",
    )
    envelopes = [e for e in backend.fetch_envelopes("Ärger & Co")]
    subjects = [
        str(email.message_from_bytes(e.headers or b"").get("Subject", "")) for e in envelopes
    ]
    assert "Beschwerde" in subjects


def test_status_counts_what_the_folder_actually_holds(backend, seeded, out_of_band):
    """The sync's progress total comes from STATUS, so STATUS has to agree with the mailbox.

    Asked of a real server rather than of the fake, because the fake counts a list it holds
    and this counts what Dovecot says without opening the folder.
    """
    ensure_folder(out_of_band, "Zähler")
    counts = backend.message_counts(["INBOX", "Zähler"])
    assert counts["INBOX"] == len(CORPUS)
    assert counts["Zähler"] == 0

    # And selecting it afterwards agrees, which is what the second bar is counted against.
    assert backend.select("INBOX", readonly=True).message_count == len(CORPUS)


def test_envelopes_come_back_with_headers_but_no_body(backend, seeded):
    envelopes = {e.uid: e for e in backend.fetch_envelopes("INBOX")}
    envelope = envelopes[seeded["ordinary"]]
    assert envelope.headers is not None
    assert b"Lunch on Thursday" in envelope.headers
    assert envelope.raw is None, "a sync must not drag whole bodies down"
    assert envelope.size > 0


def test_a_body_is_fetched_only_when_asked_for(backend, seeded):
    raw = backend.fetch_raw("INBOX", seeded["ordinary"])
    assert b"Are you free?" in raw


def test_parsing_survives_the_adversarial_corpus(backend, seeded):
    """Every message parses into something, including the ones designed not to."""
    from mailmind.content.findings import mechanical_findings
    from mailmind.content.parse import parse_message

    for name, uid in seeded.items():
        parsed = parse_message(backend.fetch_raw("INBOX", uid))
        findings = {f.code for f in mechanical_findings(parsed)}
        if name == "spoofed_display_name":
            assert "display_name_spoofs_address" in findings
            assert "body_only_address" in findings
        if name == "no_message_id":
            assert "no_message_id" in findings
        if name == "instruction_shaped":
            assert "link_target_mismatch" in findings
        if name == "malformed_mime":
            assert parsed.parse_status != "ok"


def test_flags_round_trip_through_a_real_store(backend, seeded):
    uid = seeded["ordinary"]
    result = backend.store_flags("INBOX", uid, (r"\Flagged",), add=True)
    assert result.changed, result.detail
    observed = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    assert r"\Flagged" in observed.flags

    result = backend.store_flags("INBOX", uid, (r"\Flagged",), add=False)
    assert result.changed
    observed = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    assert r"\Flagged" not in observed.flags


def test_a_move_reports_best_effort_and_actually_moves(backend, seeded, out_of_band):
    ensure_folder(out_of_band, "Archive")

    uid = seeded["newsletter"]
    result = backend.move("INBOX", uid, "Archive")
    assert result.changed, result.detail
    assert result.guarantee == "best_effort", (
        "MOVE has no UNCHANGEDSINCE, so it must never claim a conditional guarantee"
    )
    remaining = {e.uid for e in backend.fetch_envelopes("INBOX")}
    assert uid not in remaining
    assert len(backend.fetch_envelopes("Archive")) == 1


def test_a_move_declines_when_the_message_changed_underneath(backend, seeded, out_of_band):
    """The narrow window MOVE does offer: look immediately before."""
    ensure_folder(out_of_band, "Archive")
    uid = seeded["ordinary"]
    observed = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    stale_flags = tuple(sorted(observed.flags))

    # Somebody else touches it after we looked.
    out_of_band.select_folder("INBOX", readonly=False)
    out_of_band.add_flags([uid], [r"\Seen"])

    result = backend.move("INBOX", uid, "Archive", expected_flags=stale_flags)
    assert not result.changed
    assert "moved" in (result.detail or "")
    assert uid in {e.uid for e in backend.fetch_envelopes("INBOX")}


def test_the_premise_fingerprint_matches_what_the_server_reports(backend, seeded):
    uid = seeded["ordinary"]
    observed = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    before = flags_hash(observed.flags)
    backend.store_flags("INBOX", uid, (r"\Flagged",), add=True)
    after_envelope = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    assert flags_hash(after_envelope.flags) != before


@pytest.mark.skipif(
    "CONDSTORE" not in os.environ.get("MAILMIND_IMAP_CAPS", ""),
    reason="target does not declare CONDSTORE",
)
def test_a_conditional_store_succeeds_when_nothing_moved(backend, seeded):
    """The other half of the refusal test.

    Detection of a refusal works by the absence of an untagged FETCH, which fails towards
    refusing.  Without this test a client that declined everything would look correct.
    """
    uid = seeded["ordinary"]
    observed = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]

    result = backend.store_flags(
        "INBOX", uid, (r"\Flagged",), add=True, unchanged_since=observed.modseq
    )
    assert result.changed, result.detail
    assert result.guarantee == "conditional"
    current = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    assert r"\Flagged" in current.flags
    assert current.modseq > observed.modseq


@pytest.mark.skipif(
    "CONDSTORE" not in os.environ.get("MAILMIND_IMAP_CAPS", ""),
    reason="target does not declare CONDSTORE",
)
def test_a_conditional_store_refuses_when_the_message_moved_on(backend, seeded, out_of_band):
    uid = seeded["ordinary"]
    observed = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    assert observed.modseq is not None, "CONDSTORE is declared but no MODSEQ came back"
    stale_modseq = observed.modseq

    # Another client gets there first.
    out_of_band.select_folder("INBOX", readonly=False)
    out_of_band.add_flags([uid], [r"\Seen"])

    result = backend.store_flags(
        "INBOX", uid, (r"\Flagged",), add=True, unchanged_since=stale_modseq
    )
    assert not result.changed
    assert result.guarantee == "conditional"
    current = {e.uid: e for e in backend.fetch_envelopes("INBOX", [uid])}[uid]
    assert r"\Flagged" not in current.flags, "the server acted despite UNCHANGEDSINCE"
