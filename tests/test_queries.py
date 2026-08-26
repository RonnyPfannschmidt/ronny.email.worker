"""Naming a bundle's mail with a search instead of a list, and adding to one later.

Two rules carry the whole file.  A query is resolved once, when the bundle is proposed —
what it finds becomes the enumerated list, and nothing looks again.  And a bundle that grew
after a page was drawn cannot be accepted from that page, because the person reading it was
reading something else.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from mailmind import views
from mailmind.db import models as m
from mailmind.imap import sync
from mailmind.suggest import model as suggest
from tests.conftest import accept_as_shown


async def _from_query(scope, world, query, *, target="Archive", **kw):
    return await suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=m.Operation.move,
        query=query,
        target_container_id=world["containers"][target].id if target else None,
        summary="Mail a search found",
        reason="Filing what matches, so the whole of it can be read at once",
        **kw,
    )


async def _named(scope, world, names, *, target="Archive"):
    return await suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=m.Operation.move,
        message_ids=[world["seed"][n] for n in names],
        target_container_id=world["containers"][target].id if target else None,
        summary="Mail named outright",
        reason="Naming each one",
    )


def _subjects(bundle):
    return {s.message.subject for s in bundle.suggestions}


# ------------------------------------------------- a query names, once


async def test_a_query_becomes_an_enumerated_bundle_at_the_moment_it_is_proposed(scope, world):
    bundle = await _from_query(scope, world, "Lunch")
    assert _subjects(bundle) == {"Lunch on Thursday", "Re: Lunch on Thursday"}


async def test_a_bundle_built_from_a_query_does_not_go_looking_again_before_it_is_accepted(
    scope, world, backend
):
    """The whole reason a query is resolved now rather than kept."""
    bundle = await _from_query(scope, world, "Lunch")
    before = {s.message_id for s in bundle.suggestions}

    # A message that would have matched arrives after the bundle was made.
    backend.add_message(
        "INBOX",
        b"From: Bob <bob@example.com>\r\n"
        b"To: me@example.org\r\n"
        b"Subject: Lunch again\r\n"
        b"Date: Sat, 22 Aug 2026 09:00:00 +0000\r\n"
        b"Message-ID: <late@example.com>\r\n"
        b"\r\n"
        b"Well?\r\n",
    )
    await sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    await scope.flush()

    assert {s.message_id for s in bundle.suggestions} == before, (
        "the bundle re-ran its own search, so it is no longer the list anybody read"
    )


async def test_the_search_a_bundle_was_built_from_is_kept_with_the_bundle(scope, world):
    bundle = await _from_query(scope, world, "Lunch")
    (entry,) = bundle.payload["queries"]
    assert entry["text"] == "Lunch"
    assert entry["matched"] == 2
    assert entry["added"] == 2


async def test_naming_the_messages_and_a_query_at_once_is_refused_because_they_can_disagree(
    scope, world
):
    with pytest.raises(suggest.ProposalRefused, match="not both"):
        await _from_query(scope, world, "Lunch", message_ids=[world["seed"]["newsletter"]])


async def test_a_bundle_needs_either_its_messages_or_a_query_to_find_them(scope, world):
    with pytest.raises(suggest.ProposalRefused, match="either the messages"):
        await suggest.propose_bundle(
            scope,
            producer=world["producer"],
            account=world["account"],
            operation=m.Operation.move,
            target_container_id=world["containers"]["Archive"].id,
            summary="Nothing at all",
            reason="Naming neither",
        )


async def test_a_query_with_nothing_in_it_to_search_for_is_refused_rather_than_erroring(
    scope, world
):
    with pytest.raises(suggest.ProposalRefused, match="nothing in it to search for"):
        await _from_query(scope, world, "   ")


async def test_a_query_matching_nothing_is_refused_rather_than_making_an_empty_bundle(
    scope, world
):
    with pytest.raises(suggest.ProposalRefused, match="so there is no bundle to review"):
        await _from_query(scope, world, "borogoves")


async def test_a_query_matching_more_than_a_bundle_may_hold_is_refused_with_the_number(
    scope, world
):
    """Never the most relevant few: that is a bundle whose membership nobody chose."""
    with pytest.raises(suggest.ProposalRefused) as refused:
        await _from_query(scope, world, "Lunch", max_size=1)
    assert "2 messages match" in str(refused.value)
    assert "narrow the query" in str(refused.value)
    assert await scope.scalar(sa.select(sa.func.count()).select_from(m.Bundle)) == 0, (
        "a refused query left an empty bundle behind"
    )


# --------------------------------------- found is not named


async def test_a_query_skips_mail_already_in_the_target_where_naming_it_refuses_the_bundle(
    scope, world, backend
):
    backend.out_of_band_move("INBOX", world["uids"]["ordinary"], "Archive")
    await sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    await sync.sync_container(scope, world["account"], world["containers"]["Archive"], backend)
    await scope.flush()

    # Named outright, the same message refuses the whole bundle.
    with pytest.raises(suggest.ProposalRefused, match="already in the target"):
        await _named(scope, world, ["ordinary", "spoofed_display_name"])

    # Found by a search, it was never claimed, so it is left out and the rest proceed.
    bundle = await _from_query(scope, world, "Lunch")
    assert _subjects(bundle) == {"Re: Lunch on Thursday"}


async def test_a_query_that_only_found_mail_already_in_the_target_proposes_nothing_and_says_why(
    scope, world, backend
):
    for name in ("ordinary", "spoofed_display_name"):
        backend.out_of_band_move("INBOX", world["uids"][name], "Archive")
    await sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    await sync.sync_container(scope, world["account"], world["containers"]["Archive"], backend)
    await scope.flush()

    with pytest.raises(suggest.ProposalRefused, match="already in Archive"):
        await _from_query(scope, world, "Lunch")


async def test_what_a_query_left_out_is_told_to_the_producer_rather_than_dropped_quietly(
    scope, world, backend
):
    backend.out_of_band_move("INBOX", world["uids"]["ordinary"], "Archive")
    await sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    await sync.sync_container(scope, world["account"], world["containers"]["Archive"], backend)
    await scope.flush()

    bundle = await _from_query(scope, world, "Lunch")
    (entry,) = bundle.payload["queries"]
    assert entry["matched"] == 2
    assert entry["added"] == 1
    assert entry["skipped"] == {"already_in_target": 1}


# ---------------------------------------------------- growing one


async def test_a_bundle_can_be_grown_only_by_the_producer_that_made_it(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    somebody_else = scope.add(m.Producer(kind=m.ProducerKind.agent, name="another"))
    await scope.flush()
    with pytest.raises(suggest.ProposalRefused, match="only be added to by the producer"):
        await suggest.add_to_bundle(scope, bundle=bundle, producer=somebody_else, query="Lunch")


async def test_a_bundle_nobody_is_still_deciding_on_cannot_be_grown(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    await suggest.reject(scope, bundle, world["reviewer"], "no")
    with pytest.raises(suggest.ProposalRefused, match="rejected and cannot be added to"):
        await suggest.add_to_bundle(
            scope, bundle=bundle, producer=world["producer"], query="Lunch"
        )


async def test_a_bundle_whose_every_message_already_moved_on_cannot_be_grown_back_to_life(
    scope, world, backend
):
    """Otherwise a bundle a person once trusted stays alive with its contents replaced.

    It is still `proposed` in the database until somebody draws the queue, so growing it
    has to refresh it first and find it dead.
    """
    bundle = await _named(scope, world, ["newsletter"])
    backend.out_of_band_move("INBOX", world["uids"]["newsletter"], "Trash")
    await sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    await scope.flush()
    assert bundle.status is m.BundleStatus.proposed, "nobody has drawn the queue yet"

    with pytest.raises(suggest.ProposalRefused, match="stale and cannot be added to"):
        await suggest.add_to_bundle(
            scope, bundle=bundle, producer=world["producer"], query="Lunch"
        )
    assert bundle.status is m.BundleStatus.stale


async def test_growing_never_puts_back_what_the_reviewer_excluded(scope, world):
    bundle = await _from_query(scope, world, "Lunch")
    taken_out = next(s for s in bundle.suggestions if s.message.subject == "Lunch on Thursday")
    await suggest.exclude(scope, taken_out, world["reviewer"])

    with pytest.raises(suggest.ProposalRefused, match="nothing matching"):
        await suggest.add_to_bundle(
            scope, bundle=bundle, producer=world["producer"], query="Lunch"
        )
    assert taken_out.status is m.SuggestionStatus.excluded


async def test_growing_past_the_size_a_bundle_may_hold_is_refused_with_both_numbers(
    scope, world
):
    bundle = await _named(scope, world, ["newsletter"])
    with pytest.raises(suggest.ProposalRefused) as refused:
        await suggest.add_to_bundle(
            scope, bundle=bundle, producer=world["producer"], query="Lunch", max_size=2
        )
    assert "already holds 1" in str(refused.value)
    assert "2 more match" in str(refused.value)


async def test_growing_a_bundle_does_not_move_the_day_it_expires(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    was = bundle.expires_at
    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")
    assert bundle.expires_at == was, (
        "an agent adding to a proposal is not the person deciding to keep it longer"
    )


async def test_growing_a_bundle_keeps_the_search_that_grew_it(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")
    (entry,) = bundle.payload["queries"]
    assert entry["text"] == "Lunch"
    assert entry["added"] == 2
    assert len(bundle.suggestions) == 3


# ------------------------------------------- the premise of a review


async def test_a_bundle_that_grew_while_it_was_being_read_cannot_be_accepted_from_that_page(
    scope, world
):
    bundle = await _named(scope, world, ["newsletter"])
    read_through = suggest.shown_through(bundle)  # the page the person is looking at

    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")

    with pytest.raises(suggest.ProposalRefused) as refused:
        await suggest.accept(scope, bundle, world["reviewer"], reviewed_through=read_through)
    assert "2 more messages arrived" in str(refused.value)
    assert bundle.status is m.BundleStatus.proposed, "it was accepted anyway"


async def test_accepting_the_page_drawn_again_takes_what_arrived_along_with_the_rest(
    scope, world
):
    bundle = await _named(scope, world, ["newsletter"])
    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")

    accepted = await accept_as_shown(scope, bundle, world["reviewer"])
    assert len(accepted) == 3
    assert bundle.status is m.BundleStatus.accepted


async def test_an_accept_that_does_not_say_what_it_showed_is_refused(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    with pytest.raises(suggest.ProposalRefused, match="did not say which items"):
        await suggest.accept(scope, bundle, world["reviewer"], reviewed_through=0)


async def test_the_near_miss_is_recorded_because_it_is_the_failure_this_service_prevents(
    scope, world
):
    bundle = await _named(scope, world, ["newsletter"])
    read_through = suggest.shown_through(bundle)
    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")
    with pytest.raises(suggest.ProposalRefused):
        await suggest.accept(scope, bundle, world["reviewer"], reviewed_through=read_through)

    event = await scope.scalar(
        sa.select(m.AuditEvent).where(m.AuditEvent.verb == "review_premise_moved")
    )
    assert event is not None
    assert event.payload["reviewed_through"] == read_through


async def test_items_are_only_ever_appended_which_is_what_the_review_premise_rests_on(
    scope, world
):
    """One id can stand for a whole page only while nothing is ever removed.

    Excluding and dying are changes of status.  A future change that deleted a suggestion
    row would silently unbind `reviewed_through` from what it means, so it is pinned here.
    """
    bundle = await _from_query(scope, world, "Lunch")
    ids = [s.id for s in bundle.suggestions]

    await suggest.exclude(scope, bundle.suggestions[0], world["reviewer"])
    await suggest.add_to_bundle(
        scope, bundle=bundle, producer=world["producer"], query="news@list.example"
    )

    kept = [s.id for s in bundle.suggestions]
    assert kept[: len(ids)] == ids, "an item left the bundle, or the order moved"
    assert kept == sorted(kept), "items are not in the order they arrived"


# --------------------------------------------------- what the reviewer sees


async def test_the_review_page_marks_what_arrived_after_the_bundle_was_proposed(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")
    await scope.flush()

    detail = await views.bundle_detail(scope, bundle.id)
    late = {i["subject"] for i in detail["items"] if i["arrived_late"]}
    assert late == {"Lunch on Thursday", "Re: Lunch on Thursday"}
    assert detail["reviewed_through"] == suggest.shown_through(bundle)
    assert [q["text"] for q in detail["queries"]] == ["Lunch"]


async def test_a_bundle_nobody_grew_says_nothing_about_arriving_late(scope, world):
    bundle = await _from_query(scope, world, "Lunch")
    await scope.flush()
    detail = await views.bundle_detail(scope, bundle.id)
    assert not any(i["arrived_late"] for i in detail["items"])
    assert [i["arrived_from"] for i in detail["items"]] == ["Lunch", "Lunch"]


async def test_expiry_still_closes_a_bundle_that_was_grown(scope, world):
    bundle = await _named(scope, world, ["newsletter"])
    await suggest.add_to_bundle(scope, bundle=bundle, producer=world["producer"], query="Lunch")
    bundle.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    await scope.flush()

    assert await suggest.expire_due(scope) == 1
    assert bundle.status is m.BundleStatus.expired
