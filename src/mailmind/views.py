"""Read models shared by the MCP surface and the review UI.

Both need the same facts about the same cache, and letting them drift would mean an agent
proposing against one picture while a person reviews another.

Every listing here answers in the envelope :mod:`mailmind.paging` defines, and takes a
cursor.  A
caller wanting the whole of something passes ``limit=None``, which is what the review UI
does where a person is looking at one page of their own mail and the template does its own
paging.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa

from mailmind import paging
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


async def accounts(
    scope: TenantScope,
    allowed: set[int] | None = None,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    stmt = sa.select(m.Account)
    if allowed is not None:
        stmt = stmt.where(m.Account.id.in_(allowed or {-1}))
    guard = paging.fingerprint("accounts", sorted(allowed) if allowed is not None else None)
    at = paging.decode(cursor, kind="accounts", guard=guard)
    total = await scope.scalar(
        sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())
    )
    if "id" in at:
        stmt = stmt.where(m.Account.id > at["id"])
    stmt = stmt.order_by(m.Account.id)
    window = _window(limit)
    rows = await scope.all(stmt if window is None else stmt.limit(window))
    fetched_more = window is not None and len(rows) == window
    return paging.page(
        [
            {
                "id": a.id,
                "name": a.name,
                "backend": a.backend.value,
                "health": a.health.value,
                "health_detail": a.health_detail,
            }
            for a in (rows[:limit] if limit is not None else rows)
        ],
        total,
        kind="accounts",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=[{"id": a.id} for a in (rows[:limit] if limit is not None else rows)],
        fetched_more=fetched_more,
    )


async def containers(
    scope: TenantScope,
    account_id: int,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    """Folders of one account, in name order.

    Paged by name rather than by offset: folders appear and disappear as a bundle proposes
    one and a person accepts it, and an offset would drop a folder from one page and repeat
    another.  The tree an agent needs to pick a move target has to be complete or say that
    it is not, which is what the envelope does.
    """
    counts = dict(
        (
            await scope.execute(
                sa.select(m.Placement.container_id, sa.func.count())
                .join(m.Container, m.Placement.container_id == m.Container.id)
                .where(
                    m.Placement.gone_at.is_(None),
                    m.Placement.container_generation == m.Container.generation,
                    m.Container.account_id == account_id,
                )
                .group_by(m.Placement.container_id)
            )
        ).all()
    )
    stmt = sa.select(m.Container).where(
        m.Container.account_id == account_id,
        m.Container.selectable.is_(True),
        m.Container.discarded_at.is_(None),
    )
    guard = paging.fingerprint("containers", account_id)
    at = paging.decode(cursor, kind="containers", guard=guard)
    total = await scope.scalar(
        sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())
    )
    if "name" in at:
        stmt = stmt.where(m.Container.name > at["name"])
    stmt = stmt.order_by(m.Container.name)
    window = _window(limit)
    rows = await scope.all(stmt if window is None else stmt.limit(window))
    fetched_more = window is not None and len(rows) == window
    return paging.page(
        [
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
            for c in (rows[:limit] if limit is not None else rows)
        ],
        total,
        kind="containers",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=[{"name": c.name} for c in (rows[:limit] if limit is not None else rows)],
        fetched_more=fetched_more,
    )


async def container_delimiter(scope: TenantScope, account_id: int) -> str | None:
    """The hierarchy separator this account's server uses, as its folders report it."""
    return await scope.scalar(
        sa.select(m.Container.delimiter)
        .where(m.Container.account_id == account_id, m.Container.delimiter.is_not(None))
        .limit(1)
    )


def as_tree(folders: list[dict], delimiter: str | None) -> list[dict]:
    """The folders of :func:`containers`, nested the way their names already read.

    A server keeps flat names with a separator in them, so the hierarchy is derived here
    rather than stored — and a level that has no folder of its own is a label with nothing
    to open, because IMAP lets a parent exist only as part of its children's names.

    INBOX sorts first wherever it appears: it is where mail lands and where sorting it
    starts, and finding it under I in a list of two hundred is the thing this replaces.
    """
    roots: list[dict] = []
    index: dict[tuple[str, ...], dict] = {}

    def node_for(path: tuple[str, ...]) -> dict:
        if path not in index:
            node = index[path] = {"label": path[-1], "folder": None, "children": []}
            (roots if len(path) == 1 else node_for(path[:-1])["children"]).append(node)
        return index[path]

    for folder in folders:
        parts = tuple(folder["name"].split(delimiter)) if delimiter else (folder["name"],)
        node_for(parts)["folder"] = folder

    def sorted_in_place(nodes: list[dict]) -> None:
        nodes.sort(
            key=lambda node: (node["label"].upper() != "INBOX", node["label"].casefold())
        )
        for node in nodes:
            sorted_in_place(node["children"])

    sorted_in_place(roots)
    return roots


def flattened(tree: list[dict], depth: int = 0) -> list[dict]:
    """The tree back in one list, each node carrying how deep it sits.

    For the places a hierarchy has to fit in a control that has no nesting — the move
    target on a folder page is one long ``<select>``, and it was the same haystack the
    folder list used to be.
    """
    rows = []
    for node in tree:
        rows.append({"label": node["label"], "folder": node["folder"], "depth": depth})
        rows.extend(flattened(node["children"], depth + 1))
    return rows


#: What a message listing suggests instead of paging to the end of a mailbox.  Paging is
#: there now, and for a folder of thousands it is still the expensive way to learn what
#: one GROUP BY knows.
NARROW = "or narrow it, or use summarize_senders to see the shape of the rest"


def budget_for(limit_bytes: int | None) -> paging.Budget:
    """One response worth of bytes, spent across whatever lists it holds."""
    return paging.Budget(limit_bytes)


def _window(limit: int | None) -> int | None:
    """One row more than asked for, which is how a query learns there is a next page."""
    return None if limit is None else limit + 1


def _after_newest_first(at: dict):
    """The keyset predicate for "strictly after this row", newest first, nulls last.

    A date alone does not identify a row — a mailing list delivers a hundred messages
    inside a second — so the placement id is the tiebreaker, and both travel in the cursor.
    Undated mail sorts last as a block, which is why a cursor already inside that block
    compares only ids.
    """
    when, placement_id = at["d"], at["p"]
    if when is None:
        return sa.and_(m.Message.date_header.is_(None), m.Placement.id < placement_id)
    boundary = dt.datetime.fromisoformat(when)
    return sa.or_(
        m.Message.date_header.is_(None),
        m.Message.date_header < boundary,
        sa.and_(m.Message.date_header == boundary, m.Placement.id < placement_id),
    )


async def messages(
    scope: TenantScope,
    *,
    container_id: int,
    limit: int | None,
    cursor: str | None = None,
    offset: int = 0,
    budget: paging.Budget | None = None,
    from_address: str | None = None,
    list_id: str | None = None,
    unread_only: bool = False,
    before: str | None = None,
    since: str | None = None,
) -> dict:
    """Bounded observation, newest first.

    05: a request that would return more than the limit gets less, and is told so, rather
    than silently returning a slice that looks complete.  What was left out is now
    reachable — ``next_cursor`` continues the listing — but narrowing is still the cheaper
    answer for a folder of thousands, and the note says so.

    Paged by keyset rather than by offset.  A folder is being synced underneath this: mail
    arrives, mail is filed, and an offset asked a second time returns a window over a list
    that has shifted, so a page drops messages it should have shown and repeats ones it
    already did.  The cursor names the last row instead, and means the same thing however
    much has landed since.

    ``offset`` is the review UI's, and the two are mutually exclusive.  A person pages
    backwards as well as forwards and a cursor only points one way, so the page they are
    reading keeps arithmetic it can run in both directions; the cost — a window over a list
    that shifted while they read it — is one a person absorbs by looking, and an agent
    cannot.  Nothing on the agent surface passes it.
    """
    if cursor and offset:
        raise ValueError("pass a cursor or an offset, not both: they page differently")
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

    guard = paging.fingerprint(
        "messages", container_id, from_address, list_id, unread_only, before, since
    )
    at = paging.decode(cursor, kind="messages", guard=guard)
    total = await scope.scalar(
        sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())
    )
    if at:
        stmt = stmt.where(_after_newest_first(at))
    stmt = stmt.order_by(m.Message.date_header.desc().nullslast(), m.Placement.id.desc())
    window = _window(limit)
    if window is not None:
        stmt = stmt.limit(window).offset(offset)
    elif offset:
        stmt = stmt.offset(offset)
    rows = await scope.all(stmt)
    fetched_more = window is not None and len(rows) == window
    shown = rows[:limit] if limit is not None else rows
    listed = [await _message_row(scope, placement) for placement in shown]
    return paging.page(
        listed,
        total,
        kind="messages",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=[await _position_of(scope, p) for p in shown],
        fetched_more=fetched_more,
        steer=NARROW,
    )


async def _position_of(scope: TenantScope, placement: m.Placement) -> dict:
    message = await scope.get(m.Message, placement.message_id)
    return {
        "d": message.date_header.isoformat() if message.date_header else None,
        "p": placement.id,
    }


async def _message_row(scope: TenantScope, placement: m.Placement) -> dict:
    message = await scope.get(m.Message, placement.message_id)
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


#: Of what is left once the small parts are served, the least a body may have.  A
#: newsletter carrying three hundred links would otherwise spend the whole response on
#: them and answer with no text at all, which is the opposite of what was asked for.
_BODY_SHARE = 0.5


def _served(
    rows: list[dict], start: int, budget: paging.Budget, *, kind: str
) -> tuple[dict, int]:
    """One section of a message, from ``start``, for as long as the budget holds."""
    kept, _ = budget.fit(rows[start:])
    through = start + len(kept)
    return paging.section(kept, len(rows), kind=kind, through=through), through


async def message_detail(
    scope: TenantScope,
    message_id: int,
    *,
    include_body: bool = False,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    """One message, fitted to one response.

    This is the largest single thing the surface answers with — a newsletter body and its
    links measured 108 KB — and it is not a listing, so the parts that can run long carry
    the section shape and the response carries one cursor between them.  Hand it back and
    the next answer resumes every part that was cut, at once.

    The body is given first claim on what the lists leave, because a caller asking for a
    body and receiving three hundred link rows instead has been answered with the wrong
    thing.
    """
    message = await scope.get(m.Message, message_id)
    if message is None:
        raise LookupError(f"no message {message_id}")
    placement = await scope.scalar(
        live_placements().where(m.Placement.message_id == message_id)
    )
    budget = budget or paging.Budget(None)
    guard = paging.fingerprint("message", message_id, include_body)
    at = paging.decode(cursor, kind="message", guard=guard)

    recipients = [
        {"role": a.role.value, "address": a.address, "display_name": a.display_name}
        for a in message.addresses
    ]
    findings = await assessment_of(scope, m.SubjectKind.message, message.id)

    served_recipients, through_recipients = _served(
        recipients, at.get("r", 0), budget, kind="recipients"
    )
    served_findings, through_findings = _served(
        findings, at.get("s", 0), budget, kind="assessment"
    )

    detail = {
        "message_id": message.id,
        "subject": message.subject,
        "from_address": message.from_address,
        "from_display_name": message.from_display,
        "date": message.date_header.isoformat() if message.date_header else None,
        "container_id": placement.container_id if placement else None,
        "flags": placement.flags.split() if placement else [],
        "recipients": served_recipients,
        "list_id": message.list_id,
        "parse_status": message.parse_status.value,
        "assessment": served_findings,
    }
    more = {"r": through_recipients, "s": through_findings}
    unfinished = through_recipients < len(recipients) or through_findings < len(findings)

    if include_body:
        body = await scope.scalar(
            sa.select(m.MessageBody).where(m.MessageBody.message_id == message_id)
        )
        if body is None:
            detail["body"] = None
            detail["body_note"] = "no body cached; call request_body first"
        else:
            text = body.text_plain or body.text_from_html or ""
            links = body.links.get("links", [])
            attachments = body.attachments.get("attachments", [])
            # Held back from the body so the lists below it are not starved to nothing.
            held_back = (
                0 if budget.remaining is None else int(budget.remaining * (1 - _BODY_SHARE))
            )
            start = at.get("b", 0)
            if budget.remaining is not None:
                budget.remaining -= held_back
            chunk, body_more = budget.take_text(text, start)
            if budget.remaining is not None:
                budget.remaining += held_back
            served_attachments, through_attachments = _served(
                attachments, at.get("a", 0), budget, kind="attachments"
            )
            # Link targets travel beside their text so a disagreement is visible.
            # Nothing here is fetched: fetching a remote image tells the sender the
            # mail was read.
            served_links, through_links = _served(links, at.get("l", 0), budget, kind="links")
            detail["body"] = {
                "text": chunk,
                "text_offset": start,
                "text_returned": len(chunk),
                "text_total": len(text),
                "text_truncated": body_more,
                "links": served_links,
                "attachments": served_attachments,
            }
            more |= {"b": start + len(chunk), "l": through_links, "a": through_attachments}
            unfinished = (
                unfinished
                or body_more
                or (through_links < len(links) or through_attachments < len(attachments))
            )

    detail["next_cursor"] = paging.encode("message", guard, more) if unfinished else None
    return detail


async def assessment_of(scope: TenantScope, kind: m.SubjectKind, subject_id: int) -> list[dict]:
    rows = await scope.all(
        sa.select(m.Assessment).where(
            m.Assessment.subject_kind == kind, m.Assessment.subject_id == subject_id
        )
    )
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


async def summarize_senders(
    scope: TenantScope,
    container_id: int,
    limit: int | None = 100,
    *,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    """What an untended mailbox is actually made of.

    Without this an agent enumerates thousands of messages to learn what one GROUP BY
    knows, and hits the observation limit while doing it.

    Ordered by how much each sender accounts for, so the first page is the one worth
    reading.  The tail is reachable: the keyset runs over the grouped rows, on the count
    and then the address, because a count alone repeats across senders and would page in
    circles.
    """
    placements = live_placements(container_id).subquery()
    grouped = (
        sa.select(
            m.Message.from_address.label("from_address"),
            sa.func.coalesce(m.Message.from_address, "").label("sort_key"),
            sa.func.max(m.Message.from_display).label("display_name"),
            sa.func.count().label("n"),
            sa.func.min(m.Message.date_header).label("first_seen"),
            sa.func.max(m.Message.date_header).label("last_seen"),
            sa.func.sum(sa.case((placements.c.flags.contains("\\Seen"), 0), else_=1)).label(
                "unread"
            ),
        )
        .join(placements, placements.c.message_id == m.Message.id)
        .group_by(m.Message.from_address)
        .subquery()
    )
    guard = paging.fingerprint("senders", container_id)
    at = paging.decode(cursor, kind="senders", guard=guard)
    stmt = sa.select(grouped)
    if at:
        stmt = stmt.where(
            sa.or_(
                grouped.c.n < at["c"],
                sa.and_(grouped.c.n == at["c"], grouped.c.sort_key > at["a"]),
            )
        )
    stmt = stmt.order_by(grouped.c.n.desc(), grouped.c.sort_key)
    window = _window(limit)
    found = (await scope.execute(stmt if window is None else stmt.limit(window))).all()
    fetched_more = window is not None and len(found) == window
    shown = found[:limit] if limit is not None else found
    rows = [
        {
            "from_address": row.from_address,
            "display_name": row.display_name,
            "count": row.n,
            "first_seen": str(row.first_seen) if row.first_seen else None,
            "last_seen": str(row.last_seen) if row.last_seen else None,
            "unread": int(row.unread or 0),
        }
        for row in shown
    ]
    total = await scope.scalar(
        sa.select(sa.func.count(sa.distinct(m.Message.from_address)))
        .select_from(m.Message)
        .join(placements, placements.c.message_id == m.Message.id)
    )
    return paging.page(
        rows,
        total,
        kind="senders",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=[{"c": row.n, "a": row.sort_key} for row in shown],
        fetched_more=fetched_more,
    )


async def summarize_lists(
    scope: TenantScope,
    container_id: int,
    limit: int | None = 100,
    *,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    placements = live_placements(container_id).subquery()
    grouped = (
        sa.select(
            m.Message.list_id.label("list_id"),
            sa.func.count().label("n"),
            sa.func.max(m.Message.date_header).label("last_seen"),
            sa.func.max(sa.cast(m.Message.has_list_unsubscribe, sa.Integer)).label("unsub"),
        )
        .join(placements, placements.c.message_id == m.Message.id)
        .where(m.Message.list_id.is_not(None))
        .group_by(m.Message.list_id)
        .subquery()
    )
    guard = paging.fingerprint("lists", container_id)
    at = paging.decode(cursor, kind="lists", guard=guard)
    stmt = sa.select(grouped)
    if at:
        stmt = stmt.where(
            sa.or_(
                grouped.c.n < at["c"],
                sa.and_(grouped.c.n == at["c"], grouped.c.list_id > at["a"]),
            )
        )
    stmt = stmt.order_by(grouped.c.n.desc(), grouped.c.list_id)
    window = _window(limit)
    found = (await scope.execute(stmt if window is None else stmt.limit(window))).all()
    fetched_more = window is not None and len(found) == window
    shown = found[:limit] if limit is not None else found
    rows = [
        {
            "list_id": row.list_id,
            "count": row.n,
            "last_seen": str(row.last_seen) if row.last_seen else None,
            "has_unsubscribe": bool(row.unsub),
        }
        for row in shown
    ]
    total = await scope.scalar(
        sa.select(sa.func.count(sa.distinct(m.Message.list_id)))
        .select_from(m.Message)
        .join(placements, placements.c.message_id == m.Message.id)
        .where(m.Message.list_id.is_not(None))
    )
    return paging.page(
        rows,
        total,
        kind="lists",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=[{"c": row.n, "a": row.list_id} for row in shown],
        fetched_more=fetched_more,
    )


async def search(
    scope: TenantScope,
    query: str,
    *,
    account_ids: set[int] | None,
    limit: int | None,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    """Ranked search, paged by position in the ranking.

    The one listing here that cannot page by keyset — see
    :func:`mailmind.db.cache.search_messages` for why — so the cursor carries how far into
    the ranking the last page reached.  It counts rows *the index returned*, not rows this
    answered with: a hit whose message has since left every folder is dropped below, and a
    cursor that skipped it would lose a real result on the next page.
    """
    guard = paging.fingerprint(
        "search", query, sorted(account_ids) if account_ids is not None else None
    )
    at = paging.decode(cursor, kind="search", guard=guard)
    start = at.get("o", 0)
    window = _window(limit)
    ids = await cache.search_messages(
        scope, query, account_ids=account_ids, limit=window, offset=start
    )
    fetched_more = window is not None and len(ids) == window
    consumed = ids[:limit] if limit is not None else ids
    rows, positions = [], []
    for reached, message_id in enumerate(consumed, start=start + 1):
        placement = await scope.scalar(
            live_placements().where(m.Placement.message_id == message_id)
        )
        if placement is not None:
            rows.append(await _message_row(scope, placement))
            positions.append({"o": reached})
    total = await cache.count_search_messages(scope, query, account_ids=account_ids)
    return paging.page(
        rows,
        total,
        kind="messages",
        cursor_kind="search",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=positions,
        end_position={"o": start + len(consumed)},
        fetched_more=fetched_more,
        steer=NARROW,
    )


# ------------------------------------------------------------------------ bundles


async def bundle_summaries(
    scope: TenantScope,
    statuses: list[m.BundleStatus],
    *,
    account_ids: set[int] | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    """Bundles in one of the given states, newest first.

    ``decided`` is the listing that grows for the life of the service, so it is the one
    that needs a cursor most: every bundle anybody ever accepted or rejected is in it.
    """
    stmt = sa.select(m.Bundle).where(m.Bundle.status.in_(statuses))
    if account_ids is not None:
        stmt = stmt.where(m.Bundle.account_id.in_(account_ids or {-1}))
    guard = paging.fingerprint(
        "bundles",
        sorted(s.value for s in statuses),
        sorted(account_ids) if account_ids is not None else None,
    )
    at = paging.decode(cursor, kind="bundles", guard=guard)
    total = await scope.scalar(
        sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())
    )
    if at:
        boundary = dt.datetime.fromisoformat(at["t"])
        stmt = stmt.where(
            sa.or_(
                m.Bundle.created_at < boundary,
                sa.and_(m.Bundle.created_at == boundary, m.Bundle.id < at["i"]),
            )
        )
    stmt = stmt.order_by(m.Bundle.created_at.desc(), m.Bundle.id.desc())
    window = _window(limit)
    found = await scope.all(stmt if window is None else stmt.limit(window))
    fetched_more = window is not None and len(found) == window
    bundles = found[:limit] if limit is not None else found
    rows = [
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
    return paging.page(
        rows,
        total,
        kind="bundles",
        budget=budget or paging.Budget(None),
        guard=guard,
        positions=[{"t": b.created_at.isoformat(), "i": b.id} for b in bundles],
        fetched_more=fetched_more,
    )


async def proposed_counts(scope: TenantScope) -> dict[int, int]:
    """How many bundles await review, per account — for the header, so waiting work in
    an account nobody is looking at is not invisible until it expires."""
    rows = (
        await scope.execute(
            sa.select(m.Bundle.account_id, sa.func.count())
            .where(m.Bundle.status == m.BundleStatus.proposed)
            .group_by(m.Bundle.account_id)
        )
    ).all()
    return dict(rows)


async def task_summaries(
    scope: TenantScope,
    account_ids: set[int] | None,
    *,
    kinds: list[m.TaskKind] | None = None,
    live_only: bool = False,
) -> list[dict]:
    """Task rows the pages render: what runs, how far along, and what failed."""
    stmt = sa.select(m.Task)
    if account_ids is not None:
        stmt = stmt.where(m.Task.account_id.in_(account_ids or {-1}))
    if kinds is not None:
        stmt = stmt.where(m.Task.kind.in_(kinds))
    if live_only:
        stmt = stmt.where(m.Task.status.in_((m.TaskStatus.queued, m.TaskStatus.running)))
    rows = await scope.all(stmt.order_by(m.Task.id.desc()))
    return [task_view(t) for t in rows]


def task_view(task: m.Task) -> dict:
    return {
        "id": task.id,
        "kind": task.kind.value,
        "status": task.status.value,
        "account_id": task.account_id,
        "subject_id": task.subject_id,
        "done": task.progress_done,
        "total": task.progress_total,
        "note": task.progress_note,
        "error": task.error,
        "result": task.result,
        "created_at": task.created_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


async def _folder_item(scope: TenantScope, suggestion: m.Suggestion) -> dict:
    """An item that names a folder instead of a message.

    What a reviewer needs of a folder is what a discard turns on: how much it holds and
    what sits under it.  Both are read fresh from the cache rather than taken from the
    premise, because the difference between them is the change the reviewer is owed.
    """
    container = suggestion.source_container
    prefix = container.name + container.delimiter if container.delimiter else None
    children = (
        await scope.all(
            sa.select(m.Container.name).where(
                m.Container.account_id == container.account_id,
                m.Container.discarded_at.is_(None),
                m.Container.name.startswith(prefix, autoescape=True),
            )
        )
        if prefix
        else []
    )
    return {
        "suggestion_id": suggestion.id,
        "message_id": None,
        "container_id": container.id,
        "container": container.name,
        "cached_messages": await staleness.live_message_count(scope, container),
        "children": sorted(children),
        "exists_on_server": container.exists_on_server,
        "status": suggestion.status.value,
        "stale_detail": suggestion.stale_detail,
        "assessment": [],
    }


async def bundle_detail(
    scope: TenantScope,
    bundle_id: int,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    budget: paging.Budget | None = None,
) -> dict:
    """One bundle: its whole effect, item by item, with premise state.

    A bundle holds up to ``max_bundle_size`` items and four hundred of them measured
    208 KB, so ``items`` is paged like everything else.  The review UI asks for all of them
    — ``limit=None`` — and has to: ``reviewed_through`` says what the page being accepted
    from actually showed, and a reviewer accepting one page of a bundle they have not read
    to the end is the thing that field exists to prevent.
    """
    bundle = await scope.get(m.Bundle, bundle_id)
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

    guard = paging.fingerprint("bundle", bundle_id)
    at = paging.decode(cursor, kind="bundle", guard=guard)
    ordered = sorted(bundle.suggestions, key=lambda s: s.id)
    after = at.get("i")
    pending = [s for s in ordered if after is None or s.id > after]
    selected = pending if limit is None else pending[:limit]
    fetched_more = limit is not None and len(pending) > len(selected)

    items = []
    for suggestion in selected:
        if suggestion.message_id is None:
            items.append(await _folder_item(scope, suggestion))
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
                "assessment": await assessment_of(scope, m.SubjectKind.message, message.id),
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

    budget = budget or paging.Budget(None)
    paged = paging.page(
        items,
        len(ordered),
        kind="items",
        # The rows are items; the cursor is the bundle's.  Without this the cursor is
        # issued under one name and read back under another, and every second page is
        # refused as somebody else's.
        cursor_kind="bundle",
        budget=budget,
        guard=guard,
        positions=[{"i": s.id} for s in selected],
        fetched_more=fetched_more,
    )

    attempts = await scope.all(
        sa.select(m.ApplyAttempt).where(
            m.ApplyAttempt.suggestion_id.in_([s.id for s in selected] or [-1])
        )
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
        "items": paged,
        #: The last item this page is showing.  Travels back on the accept form so that
        #: accepting means "the bundle I read" rather than "the bundle as it stands".
        #: Taken from what was actually returned, not from the bundle: a caller that asked
        #: for one page of it has read one page of it.
        "reviewed_through": max((s.id for s in selected[: paged["returned"]]), default=0),
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
            for a in attempts
        ],
    }
