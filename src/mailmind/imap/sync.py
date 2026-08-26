"""Bringing the cache into step with a mailbox.

The part that matters is not the fetching, it is what happens when identity breaks.  An
IMAP folder can be recreated; when it is, every UID we remember means something else, and
everything resting on those UIDs is dead rather than merely out of date.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Protocol

import attrs
import sqlalchemy as sa

from mailmind.content.parse import parse_message
from mailmind.db import cache
from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.imap.backend import MailBackend, MessageInfo


@attrs.frozen
class SyncReport:
    container: str
    added: int = 0
    updated: int = 0
    vanished: int = 0
    identity_broken: bool = False
    suggestions_killed: int = 0
    full: bool = False


def flags_hash(flags: tuple[str, ...] | str) -> str:
    """A stable fingerprint of a flag set, for premises on servers without CONDSTORE."""
    if isinstance(flags, str):
        flags = tuple(flags.split())
    return hashlib.sha256(" ".join(sorted(flags)).encode()).hexdigest()[:32]


def discover_containers(
    scope: TenantScope, account: m.Account, backend: MailBackend
) -> list[m.Container]:
    existing = {
        c.name: c
        for c in scope.scalars(
            sa.select(m.Container).where(m.Container.account_id == account.id)
        )
    }
    out = []
    for info in backend.list_containers():
        container = existing.get(info.name)
        if container is None:
            container = m.Container(account_id=account.id, name=info.name)
            scope.add(container)
        # The server is listing it, so it is there — whoever put it there.  A row that
        # said "proposed" is adopted rather than duplicated, and one we had discarded is
        # un-discarded, because somebody has made it again and a cache that insisted
        # otherwise would be lying about a folder the person can see in their client.
        if not container.exists_on_server or container.discarded_at is not None:
            scope.audit(
                "container_adopted",
                actor_kind="service",
                subject_kind="container",
                subject_id=container.id,
                payload={
                    "name": info.name,
                    "was": "discarded" if container.discarded_at else "proposed",
                },
            )
        container.exists_on_server = True
        container.discarded_at = None
        container.delimiter = info.delimiter
        container.special_use = info.special_use
        container.selectable = info.selectable
        out.append(container)
    scope.flush()
    return out


#: How many messages a full sync asks for at once. Small enough that a first pass over a
#: long folder can say how far it has got while it is getting there, large enough that
#: twenty-nine thousand messages are not twenty-nine thousand round trips.
FETCH_BATCH = 200


class SyncProgress(Protocol):
    """Somewhere to say how far along this is, for whoever is waiting on it.

    A first sync of a real account is 187 folders and takes minutes; without this it is a
    process that prints nothing until it is finished. Kept to two calls so that the thing
    reporting progress can be a rich display, a log line, or nothing at all.
    """

    def folder_started(self, container: str, messages: int) -> None: ...

    def messages_absorbed(self, count: int) -> None: ...


def sync_container(
    scope: TenantScope,
    account: m.Account,
    container: m.Container,
    backend: MailBackend,
    *,
    force_full: bool = False,
    progress: SyncProgress | None = None,
) -> SyncReport:
    selected = backend.select(container.name, readonly=True)

    identity_broken = (
        container.uidvalidity is not None and selected.uidvalidity != container.uidvalidity
    )
    killed = 0
    if identity_broken:
        killed = break_identity(scope, container, selected.uidvalidity)
        force_full = True

    use_condstore = (
        not force_full
        and container.highestmodseq is not None
        and selected.highestmodseq is not None
        and "CONDSTORE" in backend.capabilities()
    )

    added = updated = 0

    def absorb(infos: list[MessageInfo]) -> None:
        nonlocal added, updated
        for info in infos:
            was_added = _absorb(scope, account, container, info)
            added += was_added
            updated += not was_added
        if progress is not None:
            progress.messages_absorbed(len(infos))

    if use_condstore:
        changed = backend.fetch_changed_since(container.name, container.highestmodseq)
        full = False
        if progress is not None:
            progress.folder_started(container.name, len(changed))
        absorb(changed)
        present = None
    else:
        full = True
        # Asked for in batches so that progress is something that happens during a long
        # folder rather than after it. The UID list is wanted again below to see what
        # left, so it is fetched once and passed on.
        present = backend.all_uids(container.name)
        if progress is not None:
            progress.folder_started(container.name, len(present))
        for start in range(0, len(present), FETCH_BATCH):
            batch = present[start : start + FETCH_BATCH]
            absorb(backend.fetch_envelopes(container.name, batch))

    vanished = _mark_vanished(scope, container, backend, full=full, present=present)

    container.uidvalidity = selected.uidvalidity
    container.uidnext = selected.uidnext
    container.highestmodseq = selected.highestmodseq
    container.message_count = selected.message_count
    now = dt.datetime.now(dt.UTC)
    container.last_incremental_sync_at = now
    if full:
        container.last_full_sync_at = now

    report = SyncReport(
        container=container.name,
        added=added,
        updated=updated,
        vanished=vanished,
        identity_broken=identity_broken,
        suggestions_killed=killed,
        full=full,
    )
    scope.audit(
        "container_synced",
        actor_kind="service",
        subject_kind="container",
        subject_id=container.id,
        payload=attrs.asdict(report),
    )
    return report


def break_identity(scope: TenantScope, container: m.Container, new_uidvalidity: int) -> int:
    """The folder is not the folder we remember.

    Everything cached about it is suspect, so the generation moves on, every placement
    under the old one is marked gone, and every suggestion resting on one is dead.  A
    suggestion is not applied to whatever happens to be at that UID now.
    """
    now = dt.datetime.now(dt.UTC)
    old_generation = container.generation
    container.generation += 1
    container.uidvalidity = new_uidvalidity
    container.highestmodseq = None

    scope.execute(
        sa.update(m.Placement)
        .where(
            m.Placement.container_id == container.id,
            m.Placement.container_generation == old_generation,
            m.Placement.gone_at.is_(None),
        )
        .values(gone_at=now)
    )

    live = (m.SuggestionStatus.proposed, m.SuggestionStatus.accepted)
    doomed = scope.scalars(
        sa.select(m.Suggestion).where(
            m.Suggestion.source_container_id == container.id,
            m.Suggestion.premise_container_generation == old_generation,
            m.Suggestion.status.in_(live),
        )
    ).all()
    for suggestion in doomed:
        suggestion.status = m.SuggestionStatus.stale
        suggestion.stale_detail = (
            f"{container.name} was recreated (UIDVALIDITY {new_uidvalidity}); "
            "the message this referred to can no longer be identified."
        )

    scope.audit(
        "identity_broken",
        actor_kind="service",
        subject_kind="container",
        subject_id=container.id,
        payload={
            "uidvalidity": new_uidvalidity,
            "generation": container.generation,
            "suggestions_killed": len(doomed),
        },
    )
    return len(doomed)


def _absorb(
    scope: TenantScope, account: m.Account, container: m.Container, info: MessageInfo
) -> bool:
    """Fold one FETCH result into the cache.  Returns whether it was new here."""
    raw = info.raw or info.headers or b""
    # A sync fetches the header block, so say so: a multipart message read without its
    # body is not a damaged message, and its parts are not knowable from here.
    parsed = parse_message(raw, headers_only=info.raw is None)
    # RFC822.SIZE, because `raw` here is usually the header block alone.
    message, _ = cache.upsert_message(scope, account.id, parsed, size_bytes=info.size or None)
    cache.index_message(scope, message)
    cache.record_mechanical_assessment(scope, message, parsed)

    placement = scope.scalar(
        sa.select(m.Placement).where(
            m.Placement.container_id == container.id,
            m.Placement.container_generation == container.generation,
            m.Placement.uid == info.uid,
        )
    )
    created = placement is None
    if placement is None:
        placement = m.Placement(
            message_id=message.id,
            container_id=container.id,
            uid=info.uid,
            container_generation=container.generation,
        )
        scope.add(placement)
    placement.modseq = info.modseq
    placement.flags = " ".join(sorted(info.flags))
    placement.internaldate = info.internaldate
    placement.seen_at = dt.datetime.now(dt.UTC)
    placement.gone_at = None
    scope.flush()
    return created


def _mark_vanished(
    scope: TenantScope,
    container: m.Container,
    backend: MailBackend,
    *,
    full: bool,
    present: list[int] | None = None,
) -> int:
    """Notice that something left.

    A CONDSTORE fetch reports changes, not removals, so the only way to see that a message
    is gone is to ask what is still there and compare.  QRESYNC would report removals
    directly and make this unnecessary — but it is not implemented yet, so the diff runs
    unconditionally.  Skipping it on the strength of a capability the service does not
    actually use is how a moved message stays fresh forever, and a suggestion resting on
    it gets applied to a UID that now means something else.
    """
    present = set(backend.all_uids(container.name) if present is None else present)
    live = scope.scalars(
        sa.select(m.Placement).where(
            m.Placement.container_id == container.id,
            m.Placement.container_generation == container.generation,
            m.Placement.gone_at.is_(None),
        )
    ).all()
    now = dt.datetime.now(dt.UTC)
    vanished = 0
    for placement in live:
        if placement.uid not in present:
            placement.gone_at = now
            vanished += 1
    return vanished


def fetch_and_cache_body(
    scope: TenantScope,
    account: m.Account,
    container: m.Container,
    placement: m.Placement,
    backend: MailBackend,
    *,
    budget_bytes: int,
) -> str:
    """Pull a body on demand, and recompute the mechanical findings now there is one."""
    raw = backend.fetch_raw(container.name, placement.uid)
    parsed = parse_message(raw)
    message = scope.get(m.Message, placement.message_id)
    cache.record_mechanical_assessment(scope, message, parsed)
    if account.cache_bodies:
        cache.cache_body(scope, message, parsed)
        # A preview, the attachments, and whether the MIME was actually broken: all three
        # are body questions, and a sync sees headers. Nothing used to answer them
        # afterwards either.
        cache.refresh_from_body(scope, message, parsed)
        scope.flush()
        cache.evict_bodies(scope, budget_bytes)
    return parsed.body_text
