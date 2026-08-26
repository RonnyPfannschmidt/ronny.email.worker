"""Folders a bundle makes, and folders a bundle takes away.

Both are the same reviewed unit as everything else, and both turn on the same question the
rest of this service turns on: what was true when it was proposed, and is it still true
now.  For a move into a folder that does not exist yet, the answer decides whether there is
anywhere to put the mail.  For a discard, it decides whether the folder is still the empty
one somebody agreed to lose.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from mailmind import views
from mailmind.db import models as m
from mailmind.imap import apply as applier
from mailmind.imap import sync
from mailmind.suggest import model as suggest


def _move_to_new(scope, world, names, name="Receipts/2026"):
    return suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=m.Operation.move,
        message_ids=[world["seed"][n] for n in names],
        target_container_name=name,
        summary="Receipts, which belong together",
        reason="None of these is correspondence; they are records",
    )


def _discard(scope, world, containers):
    return suggest.propose_discard(
        scope,
        producer=world["producer"],
        account=world["account"],
        container_ids=[c.id for c in containers],
        summary="Empty folders left over from a reorganisation",
        reason="Nothing has been filed in any of them and nothing is under them",
    )


def _empty_folder(scope, world, backend, name):
    """A folder that exists on the server and holds nothing."""
    backend.add_folder(name)
    containers = {c.name: c for c in sync.discover_containers(scope, world["account"], backend)}
    return containers[name]


# ------------------------------------------------------- a folder that is not there yet


def test_a_move_may_name_a_folder_that_does_not_exist_yet(scope, world, backend):
    bundle = _move_to_new(scope, world, ["newsletter"])

    target = bundle.target_container
    assert target.name == "Receipts/2026"
    assert target.exists_on_server is False
    # The point of proposing: the server has not been asked for anything.
    assert "Receipts/2026" not in backend.folders
    assert backend.created == []


def test_the_folder_is_made_when_the_move_is_accepted_and_not_before(scope, world, backend):
    bundle = _move_to_new(scope, world, ["newsletter"])
    assert "Receipts/2026" not in backend.folders

    suggest.accept(scope, bundle, world["reviewer"])
    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert backend.created == ["Receipts/2026"]
    assert [a.outcome for a in attempts] == [m.ApplyOutcome.applied]
    assert bundle.status is m.BundleStatus.applied
    assert len(backend.folders["Receipts/2026"].messages) == 1
    assert bundle.target_container.exists_on_server is True


def test_two_bundles_naming_the_same_new_folder_get_the_same_one(scope, world, backend):
    first = _move_to_new(scope, world, ["newsletter"])
    second = _move_to_new(scope, world, ["ordinary"])

    assert first.target_container_id == second.target_container_id

    suggest.accept(scope, first, world["reviewer"])
    applier.apply_bundle(scope, first, backend)
    suggest.accept(scope, second, world["reviewer"])
    applier.apply_bundle(scope, second, backend)
    scope.commit()

    # Made once.  The second bundle found it already there and moved into it.
    assert backend.created == ["Receipts/2026"]
    assert len(backend.folders["Receipts/2026"].messages) == 2


def test_a_folder_somebody_made_by_hand_first_is_adopted_not_duplicated(scope, world, backend):
    bundle = _move_to_new(scope, world, ["newsletter"])
    # The person got there first, in their own mail client.
    backend.out_of_band_create("Receipts/2026")

    suggest.accept(scope, bundle, world["reviewer"])
    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert [a.outcome for a in attempts] == [m.ApplyOutcome.applied]
    assert len(backend.folders["Receipts/2026"].messages) == 1
    rows = scope.scalars(
        sa.select(m.Container).where(m.Container.name == "Receipts/2026")
    ).all()
    assert len(rows) == 1
    assert rows[0].exists_on_server is True


def test_a_sync_adopts_a_folder_that_was_only_proposed(scope, world, backend):
    bundle = _move_to_new(scope, world, ["newsletter"])
    backend.out_of_band_create("Receipts/2026")

    sync.discover_containers(scope, world["account"], backend)
    scope.commit()

    assert bundle.target_container.exists_on_server is True
    rows = scope.scalars(
        sa.select(m.Container).where(m.Container.name == "Receipts/2026")
    ).all()
    assert len(rows) == 1


def test_a_folder_the_server_refuses_stops_the_bundle_and_moves_nothing(scope, world, backend):
    bundle = _move_to_new(scope, world, ["newsletter", "ordinary"])
    backend.refuse_create.add("Receipts/2026")

    suggest.accept(scope, bundle, world["reviewer"])
    with pytest.raises(applier.NotApplicable, match="could not be created"):
        applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    # Nothing moved, and the bundle does not claim it did.
    assert len(backend.folders["INBOX"].messages) == len(world["uids"])
    assert bundle.status is m.BundleStatus.accepted
    assert scope.scalar(sa.select(sa.func.count()).select_from(m.ApplyAttempt)) == 0


def test_a_folder_name_that_could_not_work_is_refused_at_proposal(scope, world):
    for name in ("", "   ", "/leading", "trailing/", "two//levels", "inbox", "a\x00b"):
        with pytest.raises(suggest.ProposalRefused):
            _move_to_new(scope, world, ["newsletter"], name=name)


def test_naming_a_folder_that_already_exists_is_an_ordinary_move(scope, world, backend):
    bundle = _move_to_new(scope, world, ["newsletter"], name="Archive")

    assert bundle.target_container_id == world["containers"]["Archive"].id
    assert bundle.target_container.exists_on_server is True

    suggest.accept(scope, bundle, world["reviewer"])
    applier.apply_bundle(scope, bundle, backend)
    scope.commit()
    # Nothing was created; it was already there.
    assert backend.created == []


def test_naming_the_target_twice_is_refused_rather_than_guessed(scope, world):
    with pytest.raises(suggest.ProposalRefused, match="not both"):
        suggest.propose_bundle(
            scope,
            producer=world["producer"],
            account=world["account"],
            operation=m.Operation.move,
            message_ids=[world["seed"]["newsletter"]],
            target_container_id=world["containers"]["Archive"].id,
            target_container_name="Receipts/2026",
            summary="s",
            reason="r",
        )


def test_the_reviewer_is_told_the_folder_does_not_exist_yet(scope, world):
    bundle = _move_to_new(scope, world, ["newsletter"])
    scope.commit()

    detail = views.bundle_detail(scope, bundle.id)
    assert detail["target_container"] == "Receipts/2026"
    assert detail["target_is_new"] is True

    ordinary = views.bundle_detail(
        scope, _move_to_new(scope, world, ["ordinary"], name="Archive").id
    )
    assert ordinary["target_is_new"] is False


# --------------------------------------------------------------- discarding empty ones


def test_an_empty_folder_can_be_proposed_for_discard_and_nothing_happens_yet(
    scope, world, backend
):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])

    assert bundle.operation is m.Operation.discard_container
    assert len(bundle.suggestions) == 1
    item = bundle.suggestions[0]
    assert item.message_id is None
    assert item.premise_uid is None
    assert item.premise_message_count == 0
    assert item.source_container_id == old.id
    assert "Old" in backend.folders


def test_accepting_a_discard_removes_the_folder(scope, world, backend):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])

    suggest.accept(scope, bundle, world["reviewer"])
    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert [a.outcome for a in attempts] == [m.ApplyOutcome.applied]
    assert bundle.status is m.BundleStatus.applied
    assert "Old" not in backend.folders
    assert old.discarded_at is not None


def test_a_whole_branch_goes_deepest_first(scope, world, backend):
    """A server will not delete a folder with folders under it, so order is the feature."""
    parent = _empty_folder(scope, world, backend, "Old")
    child = _empty_folder(scope, world, backend, "Old/2019")
    grandchild = _empty_folder(scope, world, backend, "Old/2019/drafts")

    # Proposed shallowest-first on purpose: the applier is what has to sort it out.
    bundle = _discard(scope, world, [parent, child, grandchild])
    suggest.accept(scope, bundle, world["reviewer"])
    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert backend.deleted == ["Old/2019/drafts", "Old/2019", "Old"]
    assert all(a.outcome is m.ApplyOutcome.applied for a in attempts)
    assert not [name for name in backend.folders if name.startswith("Old")]


def test_a_parent_whose_children_are_not_in_the_bundle_is_refused(scope, world, backend):
    parent = _empty_folder(scope, world, backend, "Old")
    _empty_folder(scope, world, backend, "Old/2019")

    with pytest.raises(suggest.ProposalRefused, match="Old/2019"):
        _discard(scope, world, [parent])


def test_an_underscore_in_a_folder_name_is_not_a_wildcard(scope, world, backend):
    """Finding children is a prefix match, and a prefix match is a LIKE underneath.

    Without escaping, `Q1_2026` claims `Q1x2026` as its child and the discard is refused
    for a folder that is not under it — or, worse the other way, a real child is missed.
    """
    target = _empty_folder(scope, world, backend, "Q1_2026")
    _empty_folder(scope, world, backend, "Q1x2026/inner")

    # Refused only if something is genuinely under Q1_2026, which nothing is.
    bundle = _discard(scope, world, [target])
    suggest.accept(scope, bundle, world["reviewer"])
    applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert backend.deleted == ["Q1_2026"]
    assert "Q1x2026/inner" in backend.folders


def test_a_folder_holding_mail_is_never_offered(scope, world, backend):
    busy = _empty_folder(scope, world, backend, "Busy")
    backend.add_message("Busy", b"Subject: hi\r\nFrom: a@b.invalid\r\n\r\nhello\r\n")
    sync.sync_container(scope, world["account"], busy, backend)

    with pytest.raises(suggest.ProposalRefused, match="holds 1 messages"):
        _discard(scope, world, [busy])


def test_inbox_and_the_special_folders_are_refused(scope, world):
    with pytest.raises(suggest.ProposalRefused, match="INBOX"):
        _discard(scope, world, [world["containers"]["INBOX"]])
    with pytest.raises(suggest.ProposalRefused, match="trash"):
        _discard(scope, world, [world["containers"]["Trash"]])
    with pytest.raises(suggest.ProposalRefused, match="archive"):
        _discard(scope, world, [world["containers"]["Archive"]])


def test_mail_arriving_before_review_kills_the_item(scope, world, backend):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])

    # A filter nobody remembers writing files something into it.
    backend.add_message("Old", b"Subject: late\r\nFrom: a@b.invalid\r\n\r\nhello\r\n")
    sync.sync_container(scope, world["account"], old, backend)

    with pytest.raises(suggest.ProposalRefused, match="filled up"):
        suggest.accept(scope, bundle, world["reviewer"])
    assert bundle.suggestions[0].status is m.SuggestionStatus.stale
    assert "no longer empty" in bundle.suggestions[0].stale_detail
    assert "Old" in backend.folders


def test_mail_arriving_after_acceptance_is_caught_by_the_server_check(scope, world, backend):
    """The second check, which is the one that matters: a person has already said yes."""
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])
    suggest.accept(scope, bundle, world["reviewer"])

    # Between accepting and applying, and the cache never hears about it.
    backend.add_message("Old", b"Subject: late\r\nFrom: a@b.invalid\r\n\r\nhello\r\n")

    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert [a.outcome for a in attempts] == [m.ApplyOutcome.refused_stale]
    assert "now holds 1 messages" in attempts[0].server_response
    assert "Old" in backend.folders
    assert old.discarded_at is None


def test_a_folder_somebody_else_already_deleted_is_reported_not_claimed(scope, world, backend):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])
    suggest.accept(scope, bundle, world["reviewer"])

    backend.out_of_band_delete("Old")

    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert [a.outcome for a in attempts] == [m.ApplyOutcome.refused_stale]
    assert "already gone" in attempts[0].server_response


def test_discarding_a_folder_that_was_never_made_asks_the_server_for_nothing(
    scope, world, backend
):
    """A proposed folder nobody accepted is a row and nothing else."""
    move = _move_to_new(scope, world, ["newsletter"])
    target = move.target_container
    suggest.reject(scope, move, world["reviewer"], "on reflection, no")

    bundle = _discard(scope, world, [target])
    suggest.accept(scope, bundle, world["reviewer"])
    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert [a.outcome for a in attempts] == [m.ApplyOutcome.applied]
    assert backend.deleted == []
    assert target.discarded_at is not None


def test_a_discarded_folder_leaves_the_container_list(scope, world, backend):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])
    suggest.accept(scope, bundle, world["reviewer"])
    applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    names = [c["name"] for c in views.containers(scope, world["account"].id)]
    assert "Old" not in names


def test_a_discarded_folder_made_again_is_revived_rather_than_collided_with(
    scope, world, backend
):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])
    suggest.accept(scope, bundle, world["reviewer"])
    applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    revived = _move_to_new(scope, world, ["newsletter"], name="Old")
    assert revived.target_container_id == old.id
    assert revived.target_container.discarded_at is None
    assert revived.target_container.exists_on_server is False

    suggest.accept(scope, revived, world["reviewer"])
    applier.apply_bundle(scope, revived, backend)
    scope.commit()
    assert len(backend.folders["Old"].messages) == 1


def test_the_reviewer_sees_folders_where_the_messages_usually_are(scope, world, backend):
    parent = _empty_folder(scope, world, backend, "Old")
    child = _empty_folder(scope, world, backend, "Old/2019")
    bundle = _discard(scope, world, [parent, child])
    scope.commit()

    detail = views.bundle_detail(scope, bundle.id)
    assert detail["operation"] == "discard_container"
    by_name = {item["container"]: item for item in detail["items"]}
    assert by_name["Old"]["children"] == ["Old/2019"]
    assert by_name["Old"]["cached_messages"] == 0
    assert by_name["Old/2019"]["children"] == []
    assert all(item["message_id"] is None for item in detail["items"])


def test_an_item_can_be_excluded_from_a_discard_like_any_other(scope, world, backend):
    keep = _empty_folder(scope, world, backend, "Keep")
    drop = _empty_folder(scope, world, backend, "Drop")
    bundle = _discard(scope, world, [keep, drop])

    excluded = next(s for s in bundle.suggestions if s.source_container_id == keep.id)
    suggest.exclude(scope, excluded, world["reviewer"])
    suggest.accept(scope, bundle, world["reviewer"])
    applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert backend.deleted == ["Drop"]
    assert "Keep" in backend.folders


def test_a_discard_reports_the_best_effort_it_actually_got(scope, world, backend):
    old = _empty_folder(scope, world, backend, "Old")
    bundle = _discard(scope, world, [old])
    suggest.accept(scope, bundle, world["reviewer"])
    attempt = applier.apply_bundle(scope, bundle, backend)[0]

    # DELETE has no UNCHANGEDSINCE.  Looking immediately before is a narrower window and
    # not a promise, and the record says so rather than implying one.
    assert attempt.precondition is m.Precondition.best_effort
    assert attempt.guarantee_obtained is m.Precondition.best_effort


def test_a_discard_cannot_be_proposed_through_propose_bundle(scope, world):
    with pytest.raises(suggest.ProposalRefused, match="over folders, not messages"):
        suggest.propose_bundle(
            scope,
            producer=world["producer"],
            account=world["account"],
            operation=m.Operation.discard_container,
            message_ids=[world["seed"]["newsletter"]],
            summary="s",
            reason="r",
        )
