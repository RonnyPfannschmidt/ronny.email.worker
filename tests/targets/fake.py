"""An in-process mail server.

Its most important method is :meth:`FakeBackend.out_of_band_mutate`.  The interesting
behaviour of this service is entirely about external change racing internal state — a
person filing a message in their own client while a suggestion about it waits for review —
and without a way to cause that race, none of the staleness machinery is actually tested.

The fake is a fake of the *backend protocol*, not of the IMAP wire.  Wire-level fidelity
is what the container targets are for; this one exists so the sync, staleness and apply
logic can be driven deterministically and without sleeping.
"""

from __future__ import annotations

import datetime as dt

import attrs

from mailmind.imap.backend import (
    ContainerInfo,
    IdentityLost,
    MailboxUnhealthy,
    MessageInfo,
    SelectInfo,
    StoreResult,
)


@attrs.define
class FakeMessage:
    uid: int
    raw: bytes
    flags: tuple[str, ...] = ()
    internaldate: dt.datetime | None = None
    modseq: int = 1


@attrs.define
class FakeFolder:
    name: str
    uidvalidity: int = 1000
    uidnext: int = 1
    highestmodseq: int = 1
    special_use: str | None = None
    messages: dict[int, FakeMessage] = attrs.field(factory=dict)


class FakeBackend:
    """A mailbox that can be made to misbehave on purpose."""

    def __init__(self, *, caps: frozenset[str] | None = None) -> None:
        self.folders: dict[str, FakeFolder] = {}
        self._caps = (
            caps
            if caps is not None
            else frozenset({"CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE"})
        )
        self.writable = True
        self.reachable = True
        self.closed = False
        #: What was created and deleted, in order.  The order is the point for a discard
        #: bundle: a parent may only go after the children that made it a parent.
        self.created: list[str] = []
        self.deleted: list[str] = []
        #: Names the server will not make.  Every server has some — a namespace it does
        #: not let you write in, a character it will not take — and none of them announce
        #: which in advance.
        self.refuse_create: set[str] = set()

    # ------------------------------------------------------------------ seeding

    def add_folder(self, name: str, *, special_use: str | None = None) -> FakeFolder:
        folder = FakeFolder(name=name, special_use=special_use)
        self.folders[name] = folder
        return folder

    def add_message(self, container: str, raw: bytes, *, flags: tuple[str, ...] = ()) -> int:
        folder = self._folder(container)
        uid = folder.uidnext
        folder.uidnext += 1
        folder.highestmodseq += 1
        folder.messages[uid] = FakeMessage(
            uid=uid,
            raw=raw,
            flags=flags,
            internaldate=dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC),
            modseq=folder.highestmodseq,
        )
        return uid

    # ------------------------------------------------------- out of band change

    def out_of_band_mutate_flags(
        self, container: str, uid: int, flags: tuple[str, ...]
    ) -> None:
        """Someone else's mail client touched this message."""
        folder = self._folder(container)
        folder.highestmodseq += 1
        message = folder.messages[uid]
        message.flags = flags
        message.modseq = folder.highestmodseq

    def out_of_band_move(self, container: str, uid: int, destination: str) -> None:
        """Someone else filed it before the reviewer got there."""
        folder = self._folder(container)
        message = folder.messages.pop(uid)
        target = self._folder(destination)
        target.highestmodseq += 1
        new_uid = target.uidnext
        target.uidnext += 1
        target.messages[new_uid] = FakeMessage(
            uid=new_uid,
            raw=message.raw,
            flags=message.flags,
            internaldate=message.internaldate,
            modseq=target.highestmodseq,
        )

    def out_of_band_create(self, name: str, *, special_use: str | None = None) -> None:
        """Somebody made the folder in their own client before we got to it."""
        self.add_folder(name, special_use=special_use)

    def out_of_band_delete(self, name: str) -> None:
        """Somebody removed the folder before we got to it."""
        self.folders.pop(name, None)

    def force_uidvalidity_change(self, container: str) -> None:
        """The folder was recreated.  Every UID we remember now means something else."""
        folder = self._folder(container)
        folder.uidvalidity += 1
        folder.uidnext = 1
        renumbered = {}
        for index, message in enumerate(folder.messages.values(), start=1):
            message.uid = index
            renumbered[index] = message
            folder.uidnext = index + 1
        folder.messages = renumbered

    def force_read_only(self) -> None:
        self.writable = False

    def force_unreachable(self) -> None:
        self.reachable = False

    def set_capabilities(self, caps: frozenset[str]) -> None:
        self._caps = caps

    # ------------------------------------------------------------- the protocol

    def capabilities(self) -> frozenset[str]:
        self._check_reachable()
        return self._caps

    def list_containers(self) -> list[ContainerInfo]:
        self._check_reachable()
        return [
            ContainerInfo(name=f.name, delimiter="/", special_use=f.special_use)
            for f in self.folders.values()
        ]

    def select(self, container: str, *, readonly: bool = True) -> SelectInfo:
        self._check_reachable()
        folder = self._folder(container)
        return SelectInfo(
            uidvalidity=folder.uidvalidity,
            uidnext=folder.uidnext,
            message_count=len(folder.messages),
            highestmodseq=folder.highestmodseq if "CONDSTORE" in self._caps else None,
        )

    def fetch_envelopes(
        self, container: str, uids: list[int] | None = None
    ) -> list[MessageInfo]:
        self._check_reachable()
        folder = self._folder(container)
        wanted = (
            folder.messages
            if uids is None
            else {uid: folder.messages[uid] for uid in uids if uid in folder.messages}
        )
        return [self._info(m, with_raw=False) for m in wanted.values()]

    def fetch_changed_since(self, container: str, modseq: int) -> list[MessageInfo]:
        self._check_reachable()
        if "CONDSTORE" not in self._caps:
            raise MailboxUnhealthy("CONDSTORE was used but is not offered")
        folder = self._folder(container)
        return [
            self._info(m, with_raw=False) for m in folder.messages.values() if m.modseq > modseq
        ]

    def message_counts(self, containers: list[str]) -> dict[str, int]:
        self._check_reachable()
        return {
            name: len(self.folders[name].messages)
            for name in containers
            if name in self.folders
        }

    def all_uids(self, container: str) -> list[int]:
        self._check_reachable()
        return sorted(self._folder(container).messages)

    def fetch_raw(self, container: str, uid: int) -> bytes:
        self._check_reachable()
        return self._folder(container).messages[uid].raw

    def store_flags(
        self,
        container: str,
        uid: int,
        flags: tuple[str, ...],
        *,
        add: bool,
        unchanged_since: int | None = None,
    ) -> StoreResult:
        self._check_writable()
        folder = self._folder(container)
        message = folder.messages.get(uid)
        if message is None:
            return StoreResult(False, "conditional", "no such uid")

        conditional = unchanged_since is not None and "CONDSTORE" in self._caps
        if conditional and message.modseq > unchanged_since:
            return StoreResult(False, "conditional", "modseq moved")

        current = set(message.flags)
        message.flags = tuple(sorted(current | set(flags) if add else current - set(flags)))
        folder.highestmodseq += 1
        message.modseq = folder.highestmodseq
        return StoreResult(True, "conditional" if conditional else "best_effort")

    def move(
        self,
        container: str,
        uid: int,
        destination: str,
        *,
        expected_modseq: int | None = None,
        expected_flags: tuple[str, ...] | None = None,
    ) -> StoreResult:
        self._check_writable()
        folder = self._folder(container)
        message = folder.messages.get(uid)
        if message is None:
            return StoreResult(False, "best_effort", "no such uid")
        # MOVE has no UNCHANGEDSINCE.  The best available is to look immediately before,
        # which is a narrower window and not a guarantee.
        if expected_modseq is not None and message.modseq != expected_modseq:
            return StoreResult(False, "best_effort", "modseq moved")
        if expected_flags is not None and tuple(sorted(message.flags)) != tuple(
            sorted(expected_flags)
        ):
            return StoreResult(False, "best_effort", "flags moved")

        target = self._folder(destination)
        del folder.messages[uid]
        target.highestmodseq += 1
        new_uid = target.uidnext
        target.uidnext += 1
        target.messages[new_uid] = FakeMessage(
            uid=new_uid,
            raw=message.raw,
            flags=message.flags,
            internaldate=message.internaldate,
            modseq=target.highestmodseq,
        )
        return StoreResult(
            True,
            "best_effort",
            resulting_uid=new_uid if "UIDPLUS" in self._caps else None,
        )

    def create_container(self, name: str) -> ContainerInfo:
        self._check_writable()
        if name in self.refuse_create:
            raise MailboxUnhealthy(f"cannot create {name!r}: the server said no")
        if name not in self.folders:
            self.add_folder(name)
        self.created.append(name)
        folder = self.folders[name]
        return ContainerInfo(name=folder.name, delimiter="/", special_use=folder.special_use)

    def delete_container(self, name: str) -> None:
        self._check_writable()
        folder = self._folder(name)
        # Both refusals a real server makes, so that the applier's ordering and its
        # emptiness check are tested against something that actually says no.
        if folder.messages:
            raise MailboxUnhealthy(
                f"cannot delete {name!r}: it holds {len(folder.messages)} messages"
            )
        # Stricter than the Dovecot the container tier runs against, deliberately.  RFC
        # 3501 lets a server go either way and that one removes the parent and leaves the
        # children orphaned, so order does not matter there and a test against it cannot
        # tell whether the applier bothers to sort.  Refusing here is what gives
        # deepest-first ordering teeth, and the applier has to work against both.
        children = [f for f in self.folders if f.startswith(name + "/")]
        if children:
            raise MailboxUnhealthy(
                f"cannot delete {name!r}: it still has {len(children)} folders under it"
            )
        del self.folders[name]
        self.deleted.append(name)

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ helpers

    def _info(self, message: FakeMessage, *, with_raw: bool = False) -> MessageInfo:
        headers, _, _ = message.raw.partition(b"\r\n\r\n")
        return MessageInfo(
            uid=message.uid,
            flags=message.flags,
            internaldate=message.internaldate,
            size=len(message.raw),
            modseq=message.modseq if "CONDSTORE" in self._caps else None,
            headers=headers + b"\r\n\r\n",
            raw=message.raw if with_raw else None,
        )

    def _folder(self, name: str) -> FakeFolder:
        try:
            return self.folders[name]
        except KeyError:
            raise IdentityLost(f"no container named {name!r}") from None

    def _check_reachable(self) -> None:
        if not self.reachable:
            raise MailboxUnhealthy("connection is down")

    def _check_writable(self) -> None:
        self._check_reachable()
        if not self.writable:
            raise MailboxUnhealthy("connection is read-only")
