"""Writing what a mailbox said into the local cache.

04 asks whether a local copy of mail state is a liability worth having.  This iteration
answers "envelopes yes, bodies only when something needs them", and an account configured
with ``cache_bodies = false`` keeps no body text at all — so the question stays answerable
by changing a setting rather than by rewriting.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa

from mailmind.content.findings import mechanical_findings
from mailmind.content.parse import ParsedMessage
from mailmind.db import models as m
from mailmind.db.scope import TenantScope


def known_sender(scope: TenantScope, account_id: int, address: str) -> bool:
    return (
        scope.scalar(
            sa.select(sa.func.count())
            .select_from(m.Message)
            .where(m.Message.account_id == account_id, m.Message.from_address == address)
        )
        or 0
    ) > 0


def upsert_message(
    scope: TenantScope, account_id: int, parsed: ParsedMessage
) -> tuple[m.Message, bool]:
    """Find or create the message row.  Returns ``(message, created)``."""
    content_key = parsed.content_key()
    message = scope.scalar(
        sa.select(m.Message).where(
            m.Message.account_id == account_id, m.Message.content_key == content_key
        )
    )
    created = message is None
    if message is None:
        message = m.Message(account_id=account_id, content_key=content_key)
        scope.add(message)

    message.message_id_header = parsed.message_id
    message.subject = parsed.subject
    message.date_header = parsed.date
    message.from_address = parsed.from_address
    message.from_display = parsed.from_display
    message.size_bytes = parsed.size_bytes
    message.has_attachments = bool(parsed.attachments)
    message.list_id = parsed.list_id
    message.has_list_unsubscribe = parsed.has_list_unsubscribe
    message.in_reply_to = parsed.in_reply_to
    message.preview = parsed.preview
    message.parse_status = m.ParseStatus(parsed.parse_status)
    scope.flush()

    if created:
        for role, address, display in parsed.addresses:
            scope.add(
                m.MessageAddress(
                    message_id=message.id,
                    role=m.AddressRole(role),
                    address=address,
                    display_name=display,
                )
            )
    return message, created


def index_message(scope: TenantScope, message: m.Message) -> None:
    """Keep the FTS row in step.

    This is the one place raw SQL reaches the database, because FTS5 is not an ORM
    entity.  The tenant therefore travels as a bound parameter rather than through the
    loader criteria, and :func:`search_messages` filters on it explicitly.
    """
    scope.session.execute(
        sa.text("DELETE FROM message_fts WHERE message_id = :mid AND tenant_id = :tid"),
        {"mid": message.id, "tid": scope.tenant_id},
    )
    scope.session.execute(
        sa.text(
            "INSERT INTO message_fts (subject, from_text, preview, message_id, "
            "tenant_id, account_id) VALUES (:subject, :from_text, :preview, :mid, "
            ":tid, :aid)"
        ),
        {
            "subject": message.subject or "",
            "from_text": " ".join(filter(None, (message.from_address, message.from_display))),
            "preview": message.preview or "",
            "mid": message.id,
            "tid": scope.tenant_id,
            "aid": message.account_id,
        },
    )


def search_messages(
    scope: TenantScope, query: str, *, account_id: int | None = None, limit: int = 50
) -> list[int]:
    sql = "SELECT message_id FROM message_fts WHERE message_fts MATCH :q AND tenant_id = :tid"
    params: dict[str, object] = {"q": query, "tid": scope.tenant_id, "limit": limit}
    if account_id is not None:
        sql += " AND account_id = :aid"
        params["aid"] = account_id
    sql += " ORDER BY rank LIMIT :limit"
    return [row[0] for row in scope.session.execute(sa.text(sql), params)]


def record_mechanical_assessment(
    scope: TenantScope, message: m.Message, parsed: ParsedMessage
) -> m.Assessment:
    """Replace this message's mechanical assessment with one computed from what we now have.

    It is recomputed when a body arrives, because header-only findings are a subset.  02
    asks whether an assessment is ever redone; here the answer is yes but only for the
    mechanical half, which cannot be argued with — a reviewer never sees an interpretation
    change underneath them.
    """
    existing = scope.scalars(
        sa.select(m.Assessment).where(
            m.Assessment.subject_kind == m.SubjectKind.message,
            m.Assessment.subject_id == message.id,
            m.Assessment.origin == m.AssessmentOrigin.mechanical,
        )
    ).all()
    for old in existing:
        scope.session.delete(old)

    assessment = m.Assessment(
        subject_kind=m.SubjectKind.message,
        subject_id=message.id,
        origin=m.AssessmentOrigin.mechanical,
    )
    scope.add(assessment)
    scope.flush()

    findings = mechanical_findings(
        parsed,
        is_known_sender=lambda address: known_sender(scope, message.account_id, address),
    )
    for finding in findings:
        scope.add(
            m.Finding(
                assessment_id=assessment.id,
                finding_class=m.FindingClass.mechanical,
                code=finding.code,
                detail=finding.detail,
                evidence=finding.evidence,
            )
        )
    return assessment


def cache_body(scope: TenantScope, message: m.Message, parsed: ParsedMessage) -> m.MessageBody:
    body = scope.scalar(sa.select(m.MessageBody).where(m.MessageBody.message_id == message.id))
    if body is None:
        body = m.MessageBody(message_id=message.id)
        scope.add(body)
    body.text_plain = parsed.text_plain
    body.text_from_html = parsed.text_from_html
    body.links = {
        "links": [{"text": link.text, "target": link.target} for link in parsed.links]
    }
    body.attachments = {
        "attachments": [
            {"filename": a.filename, "content_type": a.content_type, "size": a.size}
            for a in parsed.attachments
        ]
    }
    body.bytes_stored = len(parsed.text_plain or "") + len(parsed.text_from_html or "")
    body.cached_at = dt.datetime.now(dt.UTC)
    body.last_read_at = body.cached_at
    return body


def evict_bodies(scope: TenantScope, budget_bytes: int) -> int:
    """Drop the least recently read bodies until the cache fits.  Returns rows removed."""
    total = scope.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(m.MessageBody.bytes_stored), 0))
    )
    removed = 0
    if total <= budget_bytes:
        return 0
    for body in scope.scalars(
        sa.select(m.MessageBody).order_by(m.MessageBody.last_read_at.asc())
    ):
        if total <= budget_bytes:
            break
        total -= body.bytes_stored
        scope.session.delete(body)
        removed += 1
    return removed
