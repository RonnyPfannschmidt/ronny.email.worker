"""IMAP, spoken through :mod:`imapclient`.

Everything server-specific lives here.  What leaves this module is containers, uids,
modseqs and — importantly — which guarantee an operation actually obtained.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError
from imapclient.imapclient import join_message_ids, seq_to_parenstr
from imapclient.response_parser import parse_fetch_response

from mailmind.config import AccountConfig, ConfigError
from mailmind.imap.backend import (
    SPECIAL_USE,
    ContainerInfo,
    IdentityLost,
    MailboxUnhealthy,
    MessageInfo,
    SelectInfo,
    StoreResult,
)

#: Enough to identify, group and assess a message without pulling its body.
ENVELOPE_ITEMS = ["UID", "FLAGS", "INTERNALDATE", "RFC822.SIZE", "BODY.PEEK[HEADER]"]
_HEADER_KEY = b"BODY[HEADER]"


def _text(value: Any) -> str:
    return value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)


def normalise_special_use(flags: Iterable[Any]) -> str | None:
    """Which special use a LIST response claims, or None.

    RFC 6154 attributes are case-insensitive on the wire and servers differ, so the case
    a server happens to send is not something anything downstream should have to know:
    what leaves here is one of :data:`~mailmind.imap.backend.SPECIAL_USE`.
    """
    for flag in flags:
        name = _text(flag).lstrip("\\").lower()
        if name in SPECIAL_USE:
            return name
    return None


class ImapBackend:
    """One connection to one account.

    Not thread-safe and not meant to be: IMAP is a stateful protocol with a selected
    folder, so a connection is used by one worker at a time.
    """

    def __init__(self, account: AccountConfig) -> None:
        self._account = account
        self._selected: str | None = None
        self._readonly = True
        try:
            password = account.login.resolve()
        except ConfigError as exc:
            raise MailboxUnhealthy(f"the password could not be read: {exc}") from exc
        try:
            self._client = IMAPClient(account.host, port=account.port, ssl=account.use_ssl)
        except (IMAPClientError, OSError) as exc:
            # A refused connection, a name that does not resolve, a certificate that does
            # not cover the host asked for: all OSError, none of them a fault in this
            # process, and all of them things the person is told rather than shown a
            # traceback of.
            raise MailboxUnhealthy(
                f"cannot reach {account.host}:{account.port}: {exc}"
            ) from exc
        try:
            self._client.login(account.login.username, password)
        except IMAPClientError as exc:
            raise MailboxUnhealthy(f"login failed: {exc}") from exc
        self._caps = frozenset(_text(c).upper() for c in self._client.capabilities())
        if "ENABLE" in self._caps and "CONDSTORE" in self._caps:
            # Without this the server is not obliged to report HIGHESTMODSEQ or MODSEQ,
            # and change detection silently degrades to comparing flags.
            try:
                self._client.enable("CONDSTORE")
            except IMAPClientError:
                pass

    # ------------------------------------------------------------------ reading

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def list_containers(self) -> list[ContainerInfo]:
        out = []
        for flags, delimiter, name in self._client.list_folders():
            flag_names = {_text(f).lower() for f in flags}
            out.append(
                ContainerInfo(
                    name=name if isinstance(name, str) else _text(name),
                    delimiter=_text(delimiter) if delimiter else None,
                    special_use=normalise_special_use(flags),
                    selectable="\\noselect" not in flag_names,
                )
            )
        return out

    def select(self, container: str, *, readonly: bool = True) -> SelectInfo:
        try:
            response = self._client.select_folder(container, readonly=readonly)
        except IMAPClientError as exc:
            raise IdentityLost(f"cannot select {container!r}: {exc}") from exc
        self._selected, self._readonly = container, readonly
        return SelectInfo(
            uidvalidity=int(response[b"UIDVALIDITY"]),
            uidnext=int(response.get(b"UIDNEXT", 0)),
            message_count=int(response.get(b"EXISTS", 0)),
            highestmodseq=(
                int(response[b"HIGHESTMODSEQ"]) if b"HIGHESTMODSEQ" in response else None
            ),
        )

    def fetch_envelopes(
        self, container: str, uids: list[int] | None = None
    ) -> list[MessageInfo]:
        self._ensure_selected(container)
        target = uids if uids is not None else self._client.search(["ALL"])
        if not target:
            return []
        items = list(ENVELOPE_ITEMS)
        if "CONDSTORE" in self._caps:
            items.append("MODSEQ")
        result = self._client.fetch(target, items)
        return [self._info(uid, data) for uid, data in result.items()]

    def fetch_changed_since(self, container: str, modseq: int) -> list[MessageInfo]:
        if "CONDSTORE" not in self._caps:
            raise MailboxUnhealthy(
                "fetch_changed_since needs CONDSTORE, which this server does not offer"
            )
        self._ensure_selected(container)
        result = self._client.fetch(
            "1:*", ENVELOPE_ITEMS + ["MODSEQ"], modifiers=[f"CHANGEDSINCE {modseq}"]
        )
        return [self._info(uid, data) for uid, data in result.items()]

    def all_uids(self, container: str) -> list[int]:
        self._ensure_selected(container)
        return sorted(int(uid) for uid in self._client.search(["ALL"]))

    def fetch_raw(self, container: str, uid: int) -> bytes:
        self._ensure_selected(container)
        result = self._client.fetch([uid], ["BODY.PEEK[]"])
        if uid not in result:
            raise IdentityLost(f"uid {uid} is no longer in {container!r}")
        return result[uid].get(b"BODY[]", b"")

    # ------------------------------------------------------------------ writing

    def store_flags(
        self,
        container: str,
        uid: int,
        flags: tuple[str, ...],
        *,
        add: bool,
        unchanged_since: int | None = None,
    ) -> StoreResult:
        self._ensure_selected(container, readonly=False)
        conditional = unchanged_since is not None and "CONDSTORE" in self._caps
        try:
            if conditional:
                changed, detail = self._conditional_store(uid, flags, add, unchanged_since)
                if not changed:
                    # The server declined because the message moved on.  That is the
                    # whole point of asking conditionally.
                    return StoreResult(False, "conditional", detail or "message changed")
                return StoreResult(True, "conditional")
            if add:
                self._client.add_flags([uid], list(flags))
            else:
                self._client.remove_flags([uid], list(flags))
        except IMAPClientError as exc:
            return StoreResult(False, "conditional" if conditional else "best_effort", str(exc))
        return StoreResult(True, "best_effort")

    def _conditional_store(
        self, uid: int, flags: tuple[str, ...], add: bool, unchanged_since: int
    ) -> tuple[bool, str | None]:
        """``UID STORE <uid> (UNCHANGEDSINCE n) +FLAGS (...)``.

        imapclient offers no UNCHANGEDSINCE parameter, so the command is issued directly.
        A server that declines sends no untagged FETCH for the message and reports
        ``[MODIFIED ...]``; absence of the FETCH is the reliable half of that, and it
        fails towards refusing rather than towards claiming success.
        """
        command = b"+FLAGS" if add else b"-FLAGS"
        # imaplib's UID STORE takes exactly (message-set, op, flags), so the modifier
        # travels inside the op rather than as a fourth argument.  On the wire this is
        # ``UID STORE <set> (UNCHANGEDSINCE n) +FLAGS (\Seen)``, which is what RFC 7162
        # asks for.
        op = b"(UNCHANGEDSINCE %d) %s" % (unchanged_since, command)
        data = self._client._command_and_check(  # noqa: SLF001
            "store",
            join_message_ids([uid]),
            op,
            seq_to_parenstr(flags),
            uid=True,
        )
        for code in self._client._imap.untagged_responses.get("OK", []):  # noqa: SLF001
            if b"MODIFIED" in (code if isinstance(code, bytes) else str(code).encode()):
                return False, "MODIFIED: message changed underneath"
        try:
            updated = parse_fetch_response(data)
        except Exception:  # noqa: BLE001
            return False, "could not parse STORE response"
        return uid in updated, None if uid in updated else "no FETCH for uid; declined"

    def move(
        self,
        container: str,
        uid: int,
        destination: str,
        *,
        expected_modseq: int | None = None,
        expected_flags: tuple[str, ...] | None = None,
    ) -> StoreResult:
        self._ensure_selected(container, readonly=False)
        # MOVE has no UNCHANGEDSINCE.  The narrowest window available is to look
        # immediately before moving, and to report that as best effort rather than
        # claiming a guarantee the protocol did not give.
        if expected_modseq is not None or expected_flags is not None:
            current = self.fetch_envelopes(container, [uid])
            if not current:
                return StoreResult(False, "best_effort", "message is gone")
            observed = current[0]
            if expected_modseq is not None and observed.modseq != expected_modseq:
                return StoreResult(False, "best_effort", "modseq moved")
            if expected_flags is not None and tuple(sorted(observed.flags)) != tuple(
                sorted(expected_flags)
            ):
                return StoreResult(False, "best_effort", "flags moved")
        try:
            if "MOVE" in self._caps:
                self._client.move([uid], destination)
            else:
                self._client.copy([uid], destination)
                self._client.add_flags([uid], [r"\Deleted"])
        except IMAPClientError as exc:
            return StoreResult(False, "best_effort", str(exc))
        return StoreResult(True, "best_effort")

    def close(self) -> None:
        try:
            self._client.logout()
        except Exception:  # noqa: BLE001 — closing a broken connection is not an error
            pass

    # ------------------------------------------------------------------ helpers

    def _ensure_selected(self, container: str, *, readonly: bool = True) -> None:
        if self._selected != container or (self._readonly and not readonly):
            self.select(container, readonly=readonly)

    def _info(self, uid: int, data: dict[bytes, Any]) -> MessageInfo:
        internaldate = data.get(b"INTERNALDATE")
        if isinstance(internaldate, dt.datetime) and internaldate.tzinfo is None:
            internaldate = internaldate.replace(tzinfo=dt.UTC)
        modseq = data.get(b"MODSEQ")
        if isinstance(modseq, (tuple, list)) and modseq:
            modseq = modseq[0]
        return MessageInfo(
            uid=int(uid),
            flags=tuple(_text(f) for f in data.get(b"FLAGS", ())),
            internaldate=internaldate,
            size=int(data.get(b"RFC822.SIZE", 0)),
            modseq=int(modseq) if modseq else None,
            headers=data.get(_HEADER_KEY),
            raw=data.get(b"BODY[]"),
        )
