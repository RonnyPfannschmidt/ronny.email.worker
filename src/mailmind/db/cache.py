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


async def known_sender(
    scope: TenantScope, account_id: int, address: str, *, before_message_id: int
) -> bool:
    """Was an earlier message from this address already cached?

    "Earlier" is by insertion rather than by date, and the message being assessed is
    excluded from its own answer.  Both matter: the row is written and flushed before the
    assessment runs, so counting every message from the address counted this one and made
    ``first_contact`` a finding that could never fire — and since the mechanical half is
    recomputed when a body arrives, an answer that drifted with what has been cached
    since would quietly delete the finding from under a reviewer.
    """
    return (
        await scope.scalar(
            sa.select(sa.func.count())
            .select_from(m.Message)
            .where(
                m.Message.account_id == account_id,
                m.Message.from_address == address,
                m.Message.id < before_message_id,
            )
        )
        or 0
    ) > 0


async def upsert_message(
    scope: TenantScope,
    account_id: int,
    parsed: ParsedMessage,
    *,
    size_bytes: int | None = None,
) -> tuple[m.Message, bool]:
    """Find or create the message row.  Returns ``(message, created)``.

    ``size_bytes`` is what the server said the whole message weighs, where it said
    anything: a sync fetches headers only, so the length of the blob that was parsed is
    the header block and not the message.
    """
    content_key = parsed.content_key()
    message = await scope.scalar(
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
    message.size_bytes = parsed.size_bytes if size_bytes is None else size_bytes
    message.has_attachments = bool(parsed.attachments)
    message.list_id = parsed.list_id
    message.has_list_unsubscribe = parsed.has_list_unsubscribe
    message.in_reply_to = parsed.in_reply_to
    message.preview = parsed.preview
    message.parse_status = m.ParseStatus(parsed.parse_status)
    message.parse_detail = parsed.parse_detail
    await scope.flush()

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


async def index_message(scope: TenantScope, message: m.Message) -> None:
    """Keep the FTS row in step.

    This is the one place raw SQL reaches the database, because FTS5 is not an ORM
    entity.  The tenant therefore travels as a bound parameter rather than through the
    loader criteria, and :func:`search_messages` filters on it explicitly.
    """
    await scope.session.execute(
        sa.text("DELETE FROM message_fts WHERE message_id = :mid AND tenant_id = :tid"),
        {"mid": message.id, "tid": scope.tenant_id},
    )
    await scope.session.execute(
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


async def search_messages(
    scope: TenantScope,
    query: str,
    *,
    account_ids: set[int] | None = None,
    limit: int = 50,
) -> list[int]:
    """Search the index, optionally narrowed to a set of accounts.

    ``account_ids`` is how a caller's own view reaches the one query the loader criteria
    cannot police.  ``None`` means every account of the tenant, which is the reviewer's
    view and never an agent's; an empty set means no mail rather than all of it.
    """
    where, params = _search_where(scope, query, account_ids)
    if where is None:
        return []
    params["limit"] = limit
    sql = f"SELECT message_id FROM message_fts WHERE {where} ORDER BY rank LIMIT :limit"
    result = await scope.session.execute(sa.text(sql), params)
    return [row[0] for row in result]


async def count_search_messages(
    scope: TenantScope, query: str, *, account_ids: set[int] | None = None
) -> int:
    """How many the search matched, which is not how many it returned.

    05 asks for an observation that never looks complete when it is not.  The message
    listing has said so since it was written; this is the other half of the same promise.
    """
    where, params = _search_where(scope, query, account_ids)
    if where is None:
        return 0
    sql = f"SELECT count(*) FROM message_fts WHERE {where}"
    return int((await scope.session.execute(sa.text(sql), params)).scalar_one())


#: The three FTS5 operators a person might reasonably type. Everything else in a query is
#: text to look for, including the punctuation that makes an address an address.
_OPERATORS = frozenset({"AND", "OR", "NOT"})


def fts_query(text: str) -> str | None:
    """Turn what somebody typed into something FTS5 will accept.

    An agent searching a mailbox types an address, a domain, a URL — `alice@example.com`,
    `list.example`, `https://…` — and every one of those was a syntax error, because the
    query went to MATCH as written and FTS5 reads `@`, `.`, `:`, `-` and `"` as syntax. An
    error is the worst possible answer here: the caller cannot tell a broken query from a
    mailbox with nothing in it, and a model has no way to know what to fix.

    So a query is words rather than a language. Each whitespace-separated chunk becomes a
    quoted phrase — quoted, FTS5 tokenizes the punctuation away and matches the words in
    order, which is exactly what searching for an address means — and a trailing ``*``
    still asks for a prefix. ``AND``, ``OR`` and ``NOT`` in capitals are kept as operators,
    because somebody typing those means them; anywhere they would leave the expression
    dangling they are dropped instead of failing.

    Returns None when there is nothing left to search for, which is not an error either.
    """
    parts: list[str] = []
    for chunk in text.split():
        if chunk in _OPERATORS:
            # An operator with nothing to its left, or following another operator, is not
            # an operator: it is a word somebody typed at the start of a sentence.
            if parts and parts[-1] not in _OPERATORS:
                parts.append(chunk)
            continue
        prefix = chunk.endswith("*")
        body = chunk[:-1] if prefix else chunk
        body = body.replace('"', '""')
        if not body.strip():
            continue
        if parts and parts[-1] not in _OPERATORS:
            parts.append("AND")
        parts.append(f'"{body}"' + ("*" if prefix else ""))
    while parts and parts[-1] in _OPERATORS:
        parts.pop()
    return " ".join(parts) or None


def _search_where(
    scope: TenantScope, query: str, account_ids: set[int] | None
) -> tuple[str | None, dict[str, object]]:
    """The clause both of those share, so that the count counts what the search searched.

    A message the cache still holds but no folder still shows — expunged, or in a folder
    that was recreated — is left in the index and excluded here. Otherwise the count says
    forty and the listing hands back thirty, and the difference reads as truncation.
    """
    prepared = fts_query(query)
    if prepared is None:
        return None, {}
    where = (
        "message_fts MATCH :q AND tenant_id = :tid AND EXISTS ("
        "  SELECT 1 FROM placement p JOIN container c ON c.id = p.container_id"
        "  WHERE p.message_id = message_fts.message_id"
        "    AND p.gone_at IS NULL AND p.container_generation = c.generation)"
    )
    params: dict[str, object] = {"q": prepared, "tid": scope.tenant_id}
    if account_ids is not None:
        if not account_ids:
            # An empty set means no mail rather than all of it.
            return None, params
        names = [f"aid{index}" for index, _ in enumerate(sorted(account_ids))]
        placeholders = ", ".join(f":{name}" for name in names)
        where += f" AND account_id IN ({placeholders})"
        params.update(dict(zip(names, sorted(account_ids), strict=True)))
    return where, params


async def record_mechanical_assessment(
    scope: TenantScope, message: m.Message, parsed: ParsedMessage
) -> m.Assessment:
    """Replace this message's mechanical assessment with one computed from what we now have.

    It is recomputed when a body arrives, because header-only findings are a subset.  02
    asks whether an assessment is ever redone; here the answer is yes but only for the
    mechanical half, which cannot be argued with — a reviewer never sees an interpretation
    change underneath them.
    """
    existing = await scope.all(
        sa.select(m.Assessment).where(
            m.Assessment.subject_kind == m.SubjectKind.message,
            m.Assessment.subject_id == message.id,
            m.Assessment.origin == m.AssessmentOrigin.mechanical,
        )
    )
    for old in existing:
        await scope.delete(old)

    assessment = m.Assessment(
        subject_kind=m.SubjectKind.message,
        subject_id=message.id,
        origin=m.AssessmentOrigin.mechanical,
    )
    scope.add(assessment)
    await scope.flush()

    # ``mechanical_findings`` takes a sync callback and only ever asks about the sender's
    # address, so the one answer is fetched up front and the callback closes over it.
    sender_known = (
        await known_sender(
            scope, message.account_id, parsed.from_address, before_message_id=message.id
        )
        if parsed.from_address
        else False
    )
    findings = mechanical_findings(
        parsed,
        is_known_sender=lambda address: sender_known,
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


async def cache_body(
    scope: TenantScope, message: m.Message, parsed: ParsedMessage
) -> m.MessageBody:
    body = await scope.scalar(
        sa.select(m.MessageBody).where(m.MessageBody.message_id == message.id)
    )
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


async def refresh_from_body(
    scope: TenantScope, message: m.Message, parsed: ParsedMessage
) -> None:
    """Re-derive what only a body can settle, now that there is one.

    A sync sees headers: no preview, no attachments, and no way to tell a truncated
    multipart from one whose body it simply has not asked for.  Fetching the body answers
    all three, and the index has to be told about the preview.
    """
    message.preview = parsed.preview
    message.parse_status = m.ParseStatus(parsed.parse_status)
    message.parse_detail = parsed.parse_detail
    message.has_attachments = bool(parsed.attachments)
    await index_message(scope, message)


async def evict_bodies(scope: TenantScope, budget_bytes: int) -> int:
    """Drop the least recently read bodies until the cache fits.  Returns rows removed."""
    total = await scope.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(m.MessageBody.bytes_stored), 0))
    )
    removed = 0
    if total <= budget_bytes:
        return 0
    for body in await scope.all(
        sa.select(m.MessageBody).order_by(m.MessageBody.last_read_at.asc())
    ):
        if total <= budget_bytes:
            break
        total -= body.bytes_stored
        await scope.delete(body)
        removed += 1
    return removed
