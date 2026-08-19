"""Parts of the real IMAP client that can be checked without a server.

The container suite proves the wire; this proves the parsing sitting on top of it,
against the shapes imapclient actually hands over — bytes, backslashes, and whichever
case the server felt like using.
"""

from __future__ import annotations

import pytest

from mailmind.imap.backend import SPECIAL_USE, TRASH
from mailmind.imap.client import normalise_special_use


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
