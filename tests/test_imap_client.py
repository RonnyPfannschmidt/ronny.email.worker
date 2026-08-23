"""Parts of the real IMAP client that can be checked without a server.

The container suite proves the wire; this proves the parsing sitting on top of it,
against the shapes imapclient actually hands over — bytes, backslashes, and whichever
case the server felt like using.
"""

from __future__ import annotations

import socket

import pytest

from mailmind.config import AccountConfig, Login
from mailmind.imap.backend import SPECIAL_USE, TRASH, MailboxUnhealthy
from mailmind.imap.client import ImapBackend, normalise_special_use


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        # What a server actually sends, byte strings and backslashes included.
        ((b"\\HasNoChildren", b"\\Trash"), "trash"),
        ((b"\\HasNoChildren", b"\\Sent"), "sent"),
        ((b"\\Archive",), "archive"),
        # RFC 6154 attributes are case-insensitive and servers disagree about case.
        ((b"\\trash",), "trash"),
        ((b"\\TRASH",), "trash"),
        # Nothing special about an ordinary folder, and \Noselect is not a special use.
        ((b"\\HasNoChildren",), None),
        ((b"\\Noselect",), None),
        ((), None),
    ],
)
def test_a_special_use_leaves_the_client_in_one_spelling(flags, expected):
    assert normalise_special_use(flags) == expected


def test_the_spelling_the_client_produces_is_the_one_the_applier_looks_for():
    """The two ends of the delete path, held against each other.

    They were written apart: the client stored what the server sent (``Trash``) and the
    review flow looked up ``trash``, so a delete could never find anywhere to delete
    into. One constant, asserted from both sides.
    """
    assert TRASH in SPECIAL_USE
    assert normalise_special_use([b"\\Trash"]) == TRASH


def test_a_host_that_cannot_be_reached_is_unhealthy_rather_than_a_traceback(monkeypatch):
    """A refused connection, a name that does not resolve, a certificate for another host.

    All of them arrive as OSError from the constructor, and all of them mean the mailbox
    cannot be reached — which the review UI turns into a banner. Before, it was a 500 and
    a stack trace in a journal.
    """
    monkeypatch.setenv("X", "not-the-password")
    with socket.socket() as probe:  # a port nothing is listening on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    account = AccountConfig(
        name="unreachable",
        host="127.0.0.1",
        port=port,
        use_ssl=False,
        login=Login(username="u", password="env://X"),
    )
    with pytest.raises(MailboxUnhealthy) as unreachable:
        ImapBackend(account)
    assert f"cannot reach 127.0.0.1:{port}" in str(unreachable.value)


def test_a_password_that_cannot_be_read_is_unhealthy_too(monkeypatch):
    """The other way a connection never happens, and the same thing to say about it."""
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    account = AccountConfig(
        name="no-password",
        host="127.0.0.1",
        login=Login(username="u", password="env://NOT_SET_ANYWHERE"),
    )
    with pytest.raises(MailboxUnhealthy) as unreadable:
        ImapBackend(account)
    assert "the password could not be read" in str(unreadable.value)
