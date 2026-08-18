"""Read models shared by the MCP surface and the review UI.

Both need the same facts about the same cache, and letting them drift would mean an agent
proposing against one picture while a person reviews another.
"""

from __future__ import annotations

import sqlalchemy as sa

from mailmind.db import cache
from mailmind.db import models as m
from mailmind.db.scope import TenantScope


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
            "last_synced": c.last_incremental_sync_at.isoformat()
            if c.last_incremental_sync_at
            else None,
        }
        for c in scope.scalars(
            sa.select(m.Container)
            .where(m.Container.account_id == account_id, m.Container.selectable.is_(True))
            .order_by(m.Container.name)
        )
    ]


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
        stmt = stmt.where(m.Message.date_header < before)
    if since:
        stmt = stmt.where(m.Message.date_header >= since)

    total = scope.scalar(sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery()))
    rows = scope.scalars(
        stmt.order_by(m.Message.date_header.desc().nullslast()).limit(limit)
    ).all()
    return {
        "messages": [_message_row(scope, p) for p in rows],
        "returned": len(rows),
        "total_matching": total,
        "truncated": total > len(rows),
        "note": (
            f"{total} messages match; {len(rows)} returned. Narrow the request or use "
            "summarize_senders to see the shape of the rest."
            if total > len(rows)
            else None
        ),
    }


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


def summarize_senders(scope: TenantScope, container_id: int, limit: int = 100) -> list[dict]:
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
    return [
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


def summarize_lists(scope: TenantScope, container_id: int, limit: int = 100) -> list[dict]:
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
    return [
        {
            "list_id": list_id,
            "count": count,
            "last_seen": str(last) if last else None,
            "has_unsubscribe": bool(unsub),
        }
        for list_id, count, last, unsub in scope.execute(stmt)
    ]


def search(scope: TenantScope, query: str, *, account_id: int | None, limit: int) -> dict:
    ids = cache.search_messages(scope, query, account_id=account_id, limit=limit)
    rows = []
    for message_id in ids:
        placement = scope.scalar(live_placements().where(m.Placement.message_id == message_id))
        if placement is not None:
            rows.append(_message_row(scope, placement))
    return {"messages": rows, "returned": len(rows), "limit": limit}


# ------------------------------------------------------------------------ bundles


def bundle_summaries(scope: TenantScope, statuses: list[m.BundleStatus]) -> list[dict]:
    bundles = scope.scalars(
        sa.select(m.Bundle)
        .where(m.Bundle.status.in_(statuses))
        .order_by(m.Bundle.created_at.desc())
    ).all()
    return [
        {
            "bundle_id": b.id,
            "status": b.status.value,
            "operation": b.operation.value,
            "flag": b.flag,
            "target_container": b.target_container.name if b.target_container else None,
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


def bundle_detail(scope: TenantScope, bundle_id: int) -> dict:
    bundle = scope.get(m.Bundle, bundle_id)
    if bundle is None:
        raise LookupError(f"no bundle {bundle_id}")

    items = []
    for suggestion in bundle.suggestions:
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
        "producer": bundle.producer.name,
        "summary": bundle.summary,
        "reason": bundle.reason,
        "created_at": bundle.created_at.isoformat(),
        "expires_at": bundle.expires_at.isoformat(),
        "decided_at": bundle.decided_at.isoformat() if bundle.decided_at else None,
        "decided_by": bundle.decided_by.name if bundle.decided_by else None,
        "decision_reason": bundle.decision_reason,
        "items": items,
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
