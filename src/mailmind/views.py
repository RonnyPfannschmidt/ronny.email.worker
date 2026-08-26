"""Read models shared by the MCP surface and the review UI.

Both need the same facts about the same cache, and letting them drift would mean an agent
proposing against one picture while a person reviews another.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa

from mailmind.db import cache
from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.suggest import staleness


def live_placements(container_id: int | None = None):
    stmt = (
        sa.select(m.Placement)
        .join(m.Container, m.Placement.container_id == m.Container.id)
        .where(
            m.Placement.gone_at.is_(None),
            m.Placement.container_generation == m.Container.generation,
        )
    )
    if container_id is not None:
        stmt = stmt.where(m.Placement.container_id == container_id)
    return stmt


def instant(value: str, field: str) -> dt.datetime:
    """Read a caller's date or timestamp as a point in time.

    It has to become a datetime before it reaches the comparison.  Bound as a string it
    was compared to SQLite's own datetime text lexicographically, and ``2026-08-19T00:00``
    sorts after ``2026-08-19 09:00`` — so ``before`` quietly included the whole of the
    boundary day and ``since`` quietly dropped it.

    Naive input is read as UTC, which is what a caller writing a bare date means.
    """
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{field} must be an ISO 8601 date or timestamp — 2026-08-19, or "
            f"2026-08-19T09:00:00Z — got {value!r}"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def accounts(scope: TenantScope, allowed: set[int] | None = None) -> list[dict]:
    stmt = sa.select(m.Account)
    if allowed is not None:
        stmt = stmt.where(m.Account.id.in_(allowed or {-1}))
    return [
        {
            "id": a.id,
            "name": a.name,
            "backend": a.backend.value,
            "health": a.health.value,
            "health_detail": a.health_detail,
        }
        for a in scope.scalars(stmt)
    ]


def containers(scope: TenantScope, account_id: int) -> list[dict]:
    counts = dict(
        scope.execute(
            sa.select(m.Placement.container_id, sa.func.count())
            .join(m.Container, m.Placement.container_id == m.Container.id)
            .where(
                m.Placement.gone_at.is_(None),
                m.Placement.container_generation == m.Container.generation,
                m.Container.account_id == account_id,
            )
            .group_by(m.Placement.container_id)
        ).all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "special_use": c.special_use,
            "cached_messages": counts.get(c.id, 0),
            "server_message_count": c.message_count,
            # A folder some bundle has proposed and nobody has accepted yet.  Shown
            # rather than hidden, so a second bundle can file into the same one — and
            # marked, so nothing mistakes it for a folder that is actually there.
            "exists_on_server": c.exists_on_server,
            "last_synced": c.last_incremental_sync_at.isoformat()
            if c.last_incremental_sync_at
            else None,
        }
        for c in scope.scalars(
            sa.select(m.Container)
            .where(
                m.Container.account_id == account_id,
                m.Container.selectable.is_(True),
                m.Container.discarded_at.is_(None),
            )
            .order_by(m.Container.name)
        )
    ]


def bounded(rows: list[dict], total: int, *, kind: str = "messages") -> dict:
    """The one shape every bounded observation comes back in.

    05 asks that a request matching more than the limit gets less *and is told so*. Three
    surfaces answer that question — listing, searching, summarising — and they used to
    answer it in three shapes, one of which did not answer it at all.
    """
    return {
        kind: rows,
        "returned": len(rows),
        "total_matching": total,
        "truncated": total > len(rows),
        "note": (
            f"{total} {kind} match; {len(rows)} returned. Narrow the request or use "
            "summarize_senders to see the shape of the rest."
            if total > len(rows)
            else None
        ),
    }


def messages(
    scope: TenantScope,
    *,
    container_id: int,
    limit: int,
    from_address: str | None = None,
    list_id: str | None = None,
    unread_only: bool = False,
    before: str | None = None,
    since: str | None = None,
) -> dict:
    """Bounded observation.

    05: a request that would return more than the limit gets less, and is told so, rather
    than silently returning a slice that looks complete.
    """
    stmt = live_placements(container_id).join(m.Message, m.Placement.message_id == m.Message.id)
    if from_address:
        stmt = stmt.where(m.Message.from_address == from_address.lower())
    if list_id:
        stmt = stmt.where(m.Message.list_id == list_id)
    if unread_only:
        stmt = stmt.where(sa.not_(m.Placement.flags.contains("\\Seen")))
    if before:
        stmt = stmt.where(m.Message.date_header < instant(before, "before"))
    if since:
        stmt = stmt.where(m.Message.date_header >= instant(since, "since"))

    total = scope.scalar(sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery()))
    rows = scope.scalars(
        stmt.order_by(m.Message.date_header.desc().nullslast()).limit(limit)
    ).all()
    return bounded([_message_row(scope, p) for p in rows], total)


def _message_row(scope: TenantScope, placement: m.Placement) -> dict:
    message = scope.get(m.Message, placement.message_id)
    return {
        "message_id": message.id,
        "container_id": placement.container_id,
        "from_address": message.from_address,
        "from_display_name": message.from_display,
        "subject": message.subject,
        "date": message.date_header.isoformat() if message.date_header else None,
        "flags": placement.flags.split(),
        "list_id": message.list_id,
        "has_list_unsubscribe": message.has_list_unsubscribe,
        "has_attachments": message.has_attachments,
        "preview": message.preview,
        "parse_status": message.parse_status.value,
    }


def message_detail(scope: TenantScope, message_id: int, *, include_body: bool = False) -> dict:
    message = scope.get(m.Message, message_id)
    if message is None:
        raise LookupError(f"no message {message_id}")
    placement = scope.scalar(live_placements().where(m.Placement.message_id == message_id))
    detail = {
        "message_id": message.id,
        "subject": message.subject,
        "from_address": message.from_address,
        "from_display_name": message.from_display,
        "date": message.date_header.isoformat() if message.date_header else None,
        "container_id": placement.container_id if placement else None,
        "flags": placement.flags.split() if placement else [],
        "recipients": [
            {"role": a.role.value, "address": a.address, "display_name": a.display_name}
            for a in message.addresses
        ],
        "list_id": message.list_id,
        "parse_status": message.parse_status.value,
        "assessment": assessment_of(scope, m.SubjectKind.message, message.id),
    }
    if include_body:
        body = scope.scalar(
            sa.select(m.MessageBody).where(m.MessageBody.message_id == message_id)
        )
        if body is not None:
            detail["body"] = {
                "text": body.text_plain or body.text_from_html,
                # Link targets travel beside their text so a disagreement is visible.
                # Nothing here is fetched: fetching a remote image tells the sender the
                # mail was read.
                "links": body.links.get("links", []),
                "attachments": body.attachments.get("attachments", []),
            }
        else:
            detail["body"] = None
            detail["body_note"] = "no body cached; call request_body first"
    return detail


def assessment_of(scope: TenantScope, kind: m.SubjectKind, subject_id: int) -> list[dict]:
    rows = scope.scalars(
        sa.select(m.Assessment).where(
            m.Assessment.subject_kind == kind, m.Assessment.subject_id == subject_id
        )
    ).all()
    return [
        {
            "origin": a.origin.value,
            "producer": a.producer.name if a.producer else None,
            "producer_id": a.producer_id,
            "created_at": a.created_at.isoformat(),
            "findings": [
                {
                    "class": f.finding_class.value,
                    "code": f.code,
                    "detail": f.detail,
                    "evidence": f.evidence,
                }
                for f in a.findings
            ],
        }
        for a in rows
    ]


def summarize_senders(scope: TenantScope, container_id: int, limit: int = 100) -> dict:
    """What an untended mailbox is actually made of.

    Without this an agent enumerates thousands of messages to learn what one GROUP BY
    knows, and hits the observation limit while doing it.
    """
    placements = live_placements(container_id).subquery()
    stmt = (
        sa.select(
            m.Message.from_address,
            sa.func.max(m.Message.from_display),
            sa.func.count(),
            sa.func.min(m.Message.date_header),
            sa.func.max(m.Message.date_header),
            sa.func.sum(sa.case((placements.c.flags.contains("\\Seen"), 0), else_=1)),
        )
        .join(placements, placements.c.message_id == m.Message.id)
        .group_by(m.Message.from_address)
        .order_by(sa.func.count().desc())
        .limit(limit)
    )
    rows = [
        {
            "from_address": address,
            "display_name": display,
            "count": count,
            "first_seen": str(first) if first else None,
            "last_seen": str(last) if last else None,
            "unread": int(unread or 0),
        }
        for address, display, count, first, last, unread in scope.execute(stmt)
    ]
    total = scope.scalar(
        sa.select(sa.func.count(sa.distinct(m.Message.from_address)))
        .select_from(m.Message)
        .join(placements, placements.c.message_id == m.Message.id)
    )
    return bounded(rows, total, kind="senders")


def summarize_lists(scope: TenantScope, container_id: int, limit: int = 100) -> dict:
    placements = live_placements(container_id).subquery()
    stmt = (
        sa.select(
            m.Message.list_id,
            sa.func.count(),
            sa.func.max(m.Message.date_header),
            sa.func.max(sa.cast(m.Message.has_list_unsubscribe, sa.Integer)),
        )
        .join(placements, placements.c.message_id == m.Message.id)
        .where(m.Message.list_id.is_not(None))
        .group_by(m.Message.list_id)
        .order_by(sa.func.count().desc())
        .limit(limit)
    )
    rows = [
        {
            "list_id": list_id,
            "count": count,
            "last_seen": str(last) if last else None,
            "has_unsubscribe": bool(unsub),
        }
        for list_id, count, last, unsub in scope.execute(stmt)
    ]
    total = scope.scalar(
        sa.select(sa.func.count(sa.distinct(m.Message.list_id)))
        .select_from(m.Message)
        .join(placements, placements.c.message_id == m.Message.id)
        .where(m.Message.list_id.is_not(None))
    )
    return bounded(rows, total, kind="lists")


def search(scope: TenantScope, query: str, *, account_ids: set[int] | None, limit: int) -> dict:
    ids = cache.search_messages(scope, query, account_ids=account_ids, limit=limit)
    rows = []
    for message_id in ids:
        placement = scope.scalar(live_placements().where(m.Placement.message_id == message_id))
        if placement is not None:
            rows.append(_message_row(scope, placement))
    total = cache.count_search_messages(scope, query, account_ids=account_ids)
    return bounded(rows, total)


# ------------------------------------------------------------------------ bundles


def bundle_summaries(
    scope: TenantScope,
    statuses: list[m.BundleStatus],
    *,
    account_ids: set[int] | None = None,
) -> list[dict]:
    stmt = sa.select(m.Bundle).where(m.Bundle.status.in_(statuses))
    if account_ids is not None:
        stmt = stmt.where(m.Bundle.account_id.in_(account_ids or {-1}))
    bundles = scope.scalars(stmt.order_by(m.Bundle.created_at.desc())).all()
    return [
        {
            "bundle_id": b.id,
            "status": b.status.value,
            "operation": b.operation.value,
            "flag": b.flag,
            "target_container": b.target_container.name if b.target_container else None,
            "target_is_new": bool(
                b.target_container and not b.target_container.exists_on_server
            ),
            "producer": b.producer.name,
            "item_count": len(b.suggestions),
            "stale_count": sum(
                1 for s in b.suggestions if s.status is m.SuggestionStatus.stale
            ),
            "summary": b.summary,
            "created_at": b.created_at.isoformat(),
            "expires_at": b.expires_at.isoformat(),
        }
        for b in bundles
    ]


def _folder_item(scope: TenantScope, suggestion: m.Suggestion) -> dict:
    """An item that names a folder instead of a message.

    What a reviewer needs of a folder is what a discard turns on: how much it holds and
    what sits under it.  Both are read fresh from the cache rather than taken from the
    premise, because the difference between them is the change the reviewer is owed.
    """
    container = suggestion.source_container
    prefix = container.name + container.delimiter if container.delimiter else None
    children = (
        scope.scalars(
            sa.select(m.Container.name).where(
                m.Container.account_id == container.account_id,
                m.Container.discarded_at.is_(None),
                m.Container.name.startswith(prefix, autoescape=True),
            )
        ).all()
        if prefix
        else []
    )
    return {
        "suggestion_id": suggestion.id,
        "message_id": None,
        "container_id": container.id,
        "container": container.name,
        "cached_messages": staleness.live_message_count(scope, container),
        "children": sorted(children),
        "exists_on_server": container.exists_on_server,
        "status": suggestion.status.value,
        "stale_detail": suggestion.stale_detail,
        "assessment": [],
    }


def bundle_detail(scope: TenantScope, bundle_id: int) -> dict:
    bundle = scope.get(m.Bundle, bundle_id)
    if bundle is None:
        raise LookupError(f"no bundle {bundle_id}")

    queries = bundle.payload.get("queries", [])
    # Everything below the first span was named when the bundle was proposed; each span
    # after that is one search that grew it.  Ids are enough because items are only ever
    # appended.
    found_by = {}
    for entry in queries:
        span = entry.get("items") or []
        if len(span) == 2:
            found_by[(span[0], span[1])] = entry["text"]

    def arrived_from(suggestion_id: int) -> str | None:
        for (low, high), text in found_by.items():
            if low <= suggestion_id <= high:
                return text
        return None

    # A bundle can be named outright and then grown, so "arrived late" is not "not in the
    # first search" — it is "from a search that grew a bundle somebody could have read".
    grew_from = min(
        (entry["items"][0] for entry in queries if entry.get("grew") and entry.get("items")),
        default=None,
    )

    items = []
    for suggestion in bundle.suggestions:
        if suggestion.message_id is None:
            items.append(_folder_item(scope, suggestion))
            continue
        message = suggestion.message
        items.append(
            {
                "suggestion_id": suggestion.id,
                "message_id": message.id,
                "from_address": message.from_address,
                "from_display_name": message.from_display,
                "subject": message.subject,
                "date": message.date_header.isoformat() if message.date_header else None,
                "currently_in": suggestion.source_container.name,
                "would_move_to": (
                    bundle.target_container.name if bundle.target_container else None
                ),
                "status": suggestion.status.value,
                "stale_detail": suggestion.stale_detail,
                "arrived_from": arrived_from(suggestion.id),
                "arrived_late": grew_from is not None and suggestion.id >= grew_from,
                "assessment": assessment_of(scope, m.SubjectKind.message, message.id),
            }
        )

    # 02's firm rule is that an assessment does not come from the producer of the
    # suggestion.  With one agent configured that cannot be enforced, so it is stated.
    same_producer = sorted(
        {
            a["producer"]
            for item in items
            for a in item["assessment"]
            if a["producer_id"] == bundle.producer_id
        }
    )

    return {
        "bundle_id": bundle.id,
        "status": bundle.status.value,
        "operation": bundle.operation.value,
        "flag": bundle.flag,
        "target_container": bundle.target_container.name if bundle.target_container else None,
        "target_is_new": bool(
            bundle.target_container and not bundle.target_container.exists_on_server
        ),
        "producer": bundle.producer.name,
        "summary": bundle.summary,
        "reason": bundle.reason,
        "created_at": bundle.created_at.isoformat(),
        "expires_at": bundle.expires_at.isoformat(),
        "decided_at": bundle.decided_at.isoformat() if bundle.decided_at else None,
        "decided_by": bundle.decided_by.name if bundle.decided_by else None,
        "decision_reason": bundle.decision_reason,
        "items": items,
        #: The last item this page is showing.  Travels back on the accept form so that
        #: accepting means "the bundle I read" rather than "the bundle as it stands".
        "reviewed_through": max((s.id for s in bundle.suggestions), default=0),
        "queries": queries,
        "self_assessed_by": same_producer,
        "attempts": [
            {
                "suggestion_id": a.suggestion_id,
                "outcome": a.outcome.value,
                "precondition": a.precondition.value,
                "guarantee_obtained": a.guarantee_obtained.value,
                "detail": a.server_response,
            }
            for a in scope.scalars(
                sa.select(m.ApplyAttempt).where(
                    m.ApplyAttempt.suggestion_id.in_([s.id for s in bundle.suggestions] or [-1])
                )
            )
        ],
    }
