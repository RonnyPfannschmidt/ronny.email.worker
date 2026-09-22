"""Reading past the end of one answer.

An answer is bounded in bytes, because bytes are what truncates on the way to a client:
opencode cuts a tool result at around 50 KB, which a row limit never noticed.  What that
leaves out has to be reachable, and reachable exactly once — a page that skips a row is
worse than a page that never offered it, because nothing says it happened.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mailmind import paging, views
from mailmind.db import models as m
from mailmind.suggest import model as suggest

#: One block of UIDs per distinct seeding, handed out in order.
_UID_BLOCKS: dict[str, int] = {}

LOREM = (
    "Following up on the thread about quarterly planning and the budget spreadsheet we "
    "were going to review before it goes to finance, which has a hard deadline at the "
    "end of the month. Thursday at fourteen hundred still works, otherwise I can move. "
)


async def seed_messages(
    scope, world, count: int, *, container=None, dated: bool = True, tag: str = "paged"
):
    """Enough mail that one answer cannot hold it, with previews of a realistic weight."""
    container = container or world["containers"]["INBOX"]
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    # Two seedings in one test must not collide on (container, generation, uid), and
    # str.__hash__ is salted per process, so the spread has to be its own counter.
    base = 500000 + 100000 * _UID_BLOCKS.setdefault(tag, len(_UID_BLOCKS))
    made = []
    for i in range(count):
        message = scope.add(
            m.Message(
                account_id=world["account"].id,
                content_key=f"{tag}-{i:05d}",
                subject=f"[announce] Release 4.{i}, and what changed in it",
                date_header=start - dt.timedelta(minutes=i) if dated else None,
                from_address=f"sender-{i % 7}@lists.example.org",
                from_display=f"Person {i % 7} (via announce)",
                list_id=f"announce.list-{i % 3}.example.org",
                preview=LOREM[:400],
            )
        )
        await scope.flush()
        scope.add(
            m.Placement(
                message_id=message.id,
                container_id=container.id,
                uid=base + i,
                container_generation=container.generation,
                flags="\\Seen",
            )
        )
        made.append(message.id)
    await scope.flush()
    return made


async def walk(fetch, kind: str, guard_free: bool = False) -> list:
    """Every page of a listing, followed the way an agent is told to follow it."""
    seen, cursor, pages = [], None, 0
    while True:
        answer = await fetch(cursor)
        assert answer["returned"] == len(answer[kind])
        assert answer["truncated"] is (answer["next_cursor"] is not None)
        seen.extend(answer[kind])
        pages += 1
        assert pages < 500, "a cursor that never finishes is a cursor that never advances"
        if not answer["next_cursor"]:
            return seen
        cursor = answer["next_cursor"]


# --------------------------------------------------------------------- the envelope


def test_the_envelope_leads_so_a_cut_answer_still_says_it_was_cut():
    """The bug this shape exists for.

    The fields saying an answer is incomplete used to sit after the rows.  A client cutting
    at 50 KB therefore took away exactly the part that said rows were missing, and left
    JSON that no longer parsed — the worst of both, an answer that looks whole and cannot
    be read.
    """
    answer = paging.envelope([{"a": 1}], 99, kind="things", next_cursor="c")
    assert list(answer) == [
        "returned",
        "total_matching",
        "truncated",
        "next_cursor",
        "note",
        "things",
    ]


def test_a_cursor_is_refused_when_the_request_behind_it_changed():
    """Continuing the wrong question is worse than starting over, and silent."""
    cursor = paging.encode("messages", paging.fingerprint("messages", 1), {"d": None, "p": 5})
    assert paging.decode(cursor, kind="messages", guard=paging.fingerprint("messages", 1))
    with pytest.raises(paging.CursorError, match="different request"):
        paging.decode(cursor, kind="messages", guard=paging.fingerprint("messages", 2))
    with pytest.raises(paging.CursorError, match="not one of ours"):
        paging.decode("nonsense", kind="messages", guard="x")


def test_a_page_keeps_one_row_even_when_that_row_alone_overruns():
    """Otherwise the budget answers with nothing, hands out a cursor that has not moved,
    and a caller following it politely loops forever."""
    fat = {"text": "x" * 10_000}
    kept, dropped = paging.Budget(100).fit([fat, fat])
    assert kept == [fat]
    assert dropped is True


# ------------------------------------------------------------------------- listings


async def test_paging_a_folder_sees_every_message_once(scope, world):
    ids = await seed_messages(scope, world, 60)
    inbox = world["containers"]["INBOX"]

    async def fetch(cursor):
        return await views.messages(
            scope, container_id=inbox.id, limit=7, cursor=cursor, budget=paging.Budget(None)
        )

    seen = [row["message_id"] for row in await walk(fetch, "messages")]
    assert len(seen) == len(set(seen)), "a page repeated mail the one before it had shown"
    assert set(ids) <= set(seen)
    whole = await views.messages(scope, container_id=inbox.id, limit=None)
    assert seen == [row["message_id"] for row in whole["messages"]]


async def test_a_byte_budget_cuts_the_page_and_the_cursor_follows_the_cut(scope, world):
    """The subtle half: the cursor has to name the last row that *survived*.

    Taken from the last row fetched instead, every trimmed page would silently skip
    whatever the budget dropped — and nothing in the answer would say so.
    """
    await seed_messages(scope, world, 40)
    inbox = world["containers"]["INBOX"]

    async def fetch(cursor):
        return await views.messages(
            scope, container_id=inbox.id, limit=40, cursor=cursor, budget=paging.Budget(4096)
        )

    first = await fetch(None)
    assert first["returned"] < 40, "the row limit alone would have returned all forty"
    assert first["truncated"] is True

    seen = [row["message_id"] for row in await walk(fetch, "messages")]
    whole = await views.messages(scope, container_id=inbox.id, limit=None)
    assert seen == [row["message_id"] for row in whole["messages"]]


async def test_undated_mail_pages_too(scope, world):
    """Undated mail sorts last as a block, so a cursor inside it compares ids alone."""
    await seed_messages(scope, world, 10, dated=False, tag="undated")
    await seed_messages(scope, world, 5, tag="dated")
    inbox = world["containers"]["INBOX"]

    async def fetch(cursor):
        return await views.messages(scope, container_id=inbox.id, limit=3, cursor=cursor)

    seen = [row["message_id"] for row in await walk(fetch, "messages")]
    assert len(seen) == len(set(seen))
    whole = await views.messages(scope, container_id=inbox.id, limit=None)
    assert seen == [row["message_id"] for row in whole["messages"]]


async def test_a_cursor_from_another_filter_is_refused(scope, world):
    await seed_messages(scope, world, 20)
    inbox = world["containers"]["INBOX"]
    first = await views.messages(scope, container_id=inbox.id, limit=5)
    with pytest.raises(paging.CursorError):
        await views.messages(
            scope,
            container_id=inbox.id,
            limit=5,
            cursor=first["next_cursor"],
            unread_only=True,
        )


async def test_paging_the_folder_list(scope, world):
    account = world["account"]
    for i in range(30):
        scope.add(
            m.Container(
                account_id=account.id,
                name=f"Archive/2024/{i:03d}-project",
                delimiter="/",
                uidvalidity=1,
            )
        )
    await scope.flush()

    async def fetch(cursor):
        return await views.containers(scope, account.id, limit=4, cursor=cursor)

    seen = [row["name"] for row in await walk(fetch, "containers")]
    whole = await views.containers(scope, account.id)
    assert seen == [row["name"] for row in whole["containers"]]
    assert len(seen) == len(set(seen))


async def test_paging_the_sender_summary(scope, world):
    await seed_messages(scope, world, 40)
    inbox = world["containers"]["INBOX"]

    async def fetch(cursor):
        return await views.summarize_senders(scope, inbox.id, limit=2, cursor=cursor)

    seen = [row["from_address"] for row in await walk(fetch, "senders")]
    whole = await views.summarize_senders(scope, inbox.id, limit=None)
    assert seen == [row["from_address"] for row in whole["senders"]]
    assert len(seen) == len(set(seen))


async def test_paging_a_search(scope, world):
    from mailmind.db import cache

    ids = await seed_messages(scope, world, 25)
    for message_id in ids:
        await cache.index_message(scope, await scope.get(m.Message, message_id))
    await scope.flush()

    async def fetch(cursor):
        return await views.search(scope, "announce", account_ids=None, limit=4, cursor=cursor)

    seen = [row["message_id"] for row in await walk(fetch, "messages")]
    assert len(seen) == len(set(seen))
    assert set(ids) <= set(seen)


# -------------------------------------------------------------------------- bundles


async def test_paging_a_bundle_the_reviewer_still_sees_whole(scope, world):
    """Both halves of the same bundle: an agent reads it in pages, a person does not.

    ``reviewed_through`` is what makes accepting mean "the bundle I read", so a page of a
    bundle must never look like the whole of it.
    """
    ids = await seed_messages(scope, world, 30)
    bundle = await suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=m.Operation.move,
        message_ids=ids,
        summary="the announce list",
        reason="bulk mail nobody reads",
        target_container_id=world["containers"]["Archive"].id,
        max_size=500,
    )
    await scope.flush()

    async def fetch(cursor):
        return (await views.bundle_detail(scope, bundle.id, limit=6, cursor=cursor))["items"]

    seen = [item["suggestion_id"] for item in await walk(fetch, "items")]
    assert len(seen) == len(set(seen)) == 30

    whole = await views.bundle_detail(scope, bundle.id, limit=None)
    assert whole["items"]["truncated"] is False
    assert whole["reviewed_through"] == max(seen)

    first = await views.bundle_detail(scope, bundle.id, limit=6)
    assert first["reviewed_through"] < whole["reviewed_through"], (
        "a page of a bundle must not claim the reviewer saw all of it"
    )


async def test_paging_decided_bundles(scope, world):
    made = []
    for i in range(12):
        bundle = await suggest.propose_bundle(
            scope,
            producer=world["producer"],
            account=world["account"],
            operation=m.Operation.add_flag,
            message_ids=[world["seed"]["newsletter"]],
            summary=f"bundle {i}",
            reason="because",
            flag="\\Flagged",
        )
        bundle.status = m.BundleStatus.rejected
        made.append(bundle.id)
    await scope.flush()

    async def fetch(cursor):
        return await views.bundle_summaries(
            scope, [m.BundleStatus.rejected], limit=5, cursor=cursor
        )

    seen = [row["bundle_id"] for row in await walk(fetch, "bundles")]
    assert sorted(seen) == sorted(made)


# -------------------------------------------------------------------------- a message


async def test_one_message_pages_its_body_and_its_links_on_one_cursor(scope, world):
    """A newsletter measured 108 KB whole.  Five cursors to read one message would be a
    surface nobody uses, so the response carries one and it advances every part that was
    cut."""
    message_id = world["seed"]["newsletter"]
    scope.add(
        m.MessageBody(
            message_id=message_id,
            text_plain="word " * 6000,
            links={
                "links": [
                    {"text": f"Article {i}", "href": f"https://news.example.org/a-{i}"}
                    for i in range(200)
                ]
            },
            attachments={"attachments": []},
        )
    )
    await scope.flush()

    text, links, cursor, pages = "", [], None, 0
    while True:
        detail = await views.message_detail(
            scope,
            message_id,
            include_body=True,
            cursor=cursor,
            budget=paging.Budget(8192),
        )
        text += detail["body"]["text"]
        links.extend(detail["body"]["links"]["links"])
        pages += 1
        assert pages < 200
        if not detail["next_cursor"]:
            break
        cursor = detail["next_cursor"]

    assert pages > 1, "this message is meant not to fit in one answer"
    assert text == "word " * 6000
    assert [link["text"] for link in links] == [f"Article {i}" for i in range(200)]


async def test_a_finished_section_stops_calling_itself_truncated(scope, world):
    """`total > returned` is the wrong test on a continuation page.

    Page two of a message returns no recipients because page one returned them all, and
    that comparison read the empty list as more still to come.
    """
    message_id = world["seed"]["newsletter"]
    scope.add(
        m.MessageBody(
            message_id=message_id,
            text_plain="word " * 4000,
            links={"links": []},
            attachments={"attachments": []},
        )
    )
    await scope.flush()

    first = await views.message_detail(
        scope, message_id, include_body=True, budget=paging.Budget(4096)
    )
    assert first["recipients"]["truncated"] is False
    assert first["body"]["text_truncated"] is True

    second = await views.message_detail(
        scope,
        message_id,
        include_body=True,
        cursor=first["next_cursor"],
        budget=paging.Budget(4096),
    )
    assert second["recipients"]["returned"] == 0
    assert second["recipients"]["truncated"] is False, (
        "a section that is finished must not ask to be read again"
    )
    assert second["body"]["text_offset"] == first["body"]["text_returned"]


async def test_a_message_that_fits_says_there_is_no_more(scope, world):
    detail = await views.message_detail(
        scope, world["seed"]["newsletter"], budget=paging.Budget(32 * 1024)
    )
    assert detail["next_cursor"] is None
    assert detail["recipients"]["truncated"] is False
