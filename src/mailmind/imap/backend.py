"""What the rest of the service is allowed to know about a mail server.

04 says the service should not know which backend it is talking to; it should know what
that backend can promise.  This protocol is that line.  Everything above it deals in
containers, uids and modseqs; everything below it deals in IMAP, and later in whatever
Gmail turns out to need.

The promises are not uniform, so operations return what guarantee they actually obtained
rather than claiming the one that was asked for.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

import attrs


class MailboxUnhealthy(Exception):
    """Reading works and writing does not, or nothing works and someone has to look."""


class IdentityLost(Exception):
    """UIDVALIDITY changed, or a cursor expired.

    Everything remembered about the container is suspect and suggestions resting on it
    are dead.
    """


#: The RFC 6154 attributes mailmind acts on, in the one spelling the rest of the service
#: uses: lowercase, no backslash.  A backend announcing ``\Trash`` normalises before the
#: value leaves it, so nothing above this line has to know which case a server chose.
SPECIAL_USE = ("sent", "drafts", "trash", "junk", "archive")

#: Where a delete goes.  Named rather than spelled out at the call site, because the last
#: time it was spelled out it was spelled differently at each end and delete could never
#: find anywhere to delete into.
TRASH = "trash"


@attrs.frozen
class ContainerInfo:
    name: str
    delimiter: str | None = None
    #: One of :data:`SPECIAL_USE`, or None.  Normalised by the backend, never raw.
    special_use: str | None = None
    selectable: bool = True


@attrs.frozen
class SelectInfo:
    uidvalidity: int
    uidnext: int
    message_count: int
    highestmodseq: int | None = None


@attrs.frozen
class MessageInfo:
    """What a FETCH gives back without asking for a body."""

    uid: int
    flags: tuple[str, ...]
    internaldate: dt.datetime | None
    size: int
    modseq: int | None = None
    #: Headers only.  A sync fetches these for every message; bodies cost too much to
    #: pull for a whole untended mailbox and are fetched on demand instead.
    headers: bytes | None = None
    #: The whole message.  Present only when something asked for a body.
    raw: bytes | None = None


@attrs.frozen
class StoreResult:
    """What actually happened, including which promise was obtained.

    ``conditional`` means the server refused to act had the message changed.
    ``best_effort`` means it did not offer that, and the caller must say so rather than
    imply a guarantee it did not get.
    """

    changed: bool
    guarantee: str
    detail: str | None = None
    resulting_uid: int | None = None


@runtime_checkable
class MailBackend(Protocol):
    """The whole of what mailmind asks a mail server to do.

    Note what is absent: there is no send.  04 puts sending outside this surface, so it
    is not a permission that is withheld — it is a method that does not exist.
    """

    def capabilities(self) -> frozenset[str]: ...

    def list_containers(self) -> list[ContainerInfo]: ...

    def select(self, container: str, *, readonly: bool = True) -> SelectInfo: ...

    def fetch_envelopes(
        self, container: str, uids: list[int] | None = None
    ) -> list[MessageInfo]: ...

    def fetch_changed_since(self, container: str, modseq: int) -> list[MessageInfo]: ...

    def all_uids(self, container: str) -> list[int]: ...

    def message_counts(self, containers: list[str]) -> dict[str, int]:
        """How many messages each of these holds, without opening any of them."""
        ...

    def fetch_raw(self, container: str, uid: int) -> bytes: ...

    def store_flags(
        self,
        container: str,
        uid: int,
        flags: tuple[str, ...],
        *,
        add: bool,
        unchanged_since: int | None = None,
    ) -> StoreResult: ...

    def move(
        self,
        container: str,
        uid: int,
        destination: str,
        *,
        expected_modseq: int | None = None,
        expected_flags: tuple[str, ...] | None = None,
    ) -> StoreResult: ...

    def create_container(self, name: str) -> ContainerInfo:
        """Make a folder, and say what the server made.

        A name is not a folder: servers normalise, impose their own hierarchy separator,
        and sometimes decline outright.  What comes back is what is actually there, so the
        caller records that rather than the name it asked for.

        Making one that is already there is a success.  The point of the call is that the
        folder exists afterwards, and racing somebody's mail client to create it is not a
        failure of anything.
        """
        ...

    def delete_container(self, name: str) -> None:
        """Get rid of a folder.

        Only ever called on one holding nothing — which is checked here, immediately
        before, and is what makes this the one deletion in the service that cannot lose
        mail.  A server that refuses raises :class:`MailboxUnhealthy` carrying what it
        said; there is no partial outcome to report.
        """
        ...

    def close(self) -> None: ...
