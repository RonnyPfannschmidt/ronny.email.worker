"""Searching a mailbox, which is mostly a question of what somebody types.

An agent searching mail types an address, a domain, a URL. FTS5 reads most of that as
syntax, and a syntax error is the worst answer available here: the caller cannot tell a
broken query from an empty mailbox, and a model has no way to know what to fix.
"""

from __future__ import annotations

import sqlalchemy as sa

from mailmind.db import cache
from mailmind.db import models as m
from mailmind.views import live_placements, search


def test_the_query_is_words_rather_than_a_language():
    """What `fts_query` makes of what somebody typed."""
    assert cache.fts_query("alice@example.com") == '"alice@example.com"'
    assert cache.fts_query("lunch thursday") == '"lunch" AND "thursday"'
    assert cache.fts_query("lunch AND thursday") == '"lunch" AND "thursday"'
    assert cache.fts_query("lunch OR thursday") == '"lunch" OR "thursday"'
    assert cache.fts_query("lun*") == '"lun"*'
    assert cache.fts_query('say "hi"') == '"say" AND """hi"""'
    # Operators that would leave the expression dangling are words, or nothing at all.
    assert cache.fts_query("AND") is None
    assert cache.fts_query("lunch AND") == '"lunch"'
    assert cache.fts_query("") is None
    assert cache.fts_query("   ") is None


PUNCTUATION = [
    "alice@example.com",
    "news@list.example",
    "list.example",
    "https://evil.example/go",
    "C++",
    "50%",
    "a:b",
    "(unbalanced",
    'quote" inside',
    "-lunch",
    "*",
    "^lunch",
    "",
]


async def test_no_query_a_person_could_type_is_an_error(scope, world):
    """Every one of these raised OperationalError, straight out of the tool."""
    for query in PUNCTUATION:
        result = await search(scope, query, account_ids=None, limit=10)
        assert result["returned"] <= result["total_matching"], query


async def test_an_address_finds_the_mail_that_carries_it(scope, world):
    """The obvious search on a mailbox, and the one that used to be a syntax error."""
    found = await search(scope, "alice@example.com", account_ids=None, limit=10)
    assert found["total_matching"] >= 1
    assert any(row["from_address"] == "alice@example.com" for row in found["messages"])


async def test_a_body_becomes_searchable_when_it_is_fetched_and_not_before(
    scope, world, backend
):
    """A sync sees headers, so a preview cannot exist yet. Nothing used to fill it in
    afterwards either: every preview in every listing was empty, the index's preview column
    held nothing, and the tool went on describing a search over previews."""
    ordinary = await scope.get(m.Message, world["seed"]["ordinary"])
    assert ordinary.preview is None, "a header-only sync should not invent a preview"
    assert (await search(scope, "free", account_ids=None, limit=10))["total_matching"] == 0

    from mailmind.imap import sync as sync_module

    placement = await scope.scalar(
        live_placements().where(m.Placement.message_id == ordinary.id)
    )
    container = await scope.get(m.Container, placement.container_id)
    await sync_module.fetch_and_cache_body(
        scope, world["account"], container, placement, backend, budget_bytes=10_000_000
    )
    await scope.flush()

    assert (await scope.get(m.Message, ordinary.id)).preview == "Are you free?"
    assert (await search(scope, "free", account_ids=None, limit=10))["total_matching"] == 1


async def test_a_message_no_folder_still_shows_is_not_counted_as_a_match(scope, world, backend):
    """The index keeps the message; the mailbox does not. Counting it made the totals
    disagree with the rows, which reads as truncation that never happened."""
    before = await search(scope, "lunch", account_ids=None, limit=10)
    assert before["total_matching"] == before["returned"] >= 1

    ordinary = await scope.get(m.Message, world["seed"]["ordinary"])
    for placement in await scope.all(
        sa.select(m.Placement).where(m.Placement.message_id == ordinary.id)
    ):
        placement.gone_at = sa.func.now()
    await scope.flush()

    after = await search(scope, "lunch", account_ids=None, limit=10)
    assert after["total_matching"] == after["returned"] == before["returned"] - 1
    assert after["truncated"] is False
