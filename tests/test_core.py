"""The behaviour this service exists for: proposing, reviewing, and refusing to act on
something that moved.

Messages are addressed by logical corpus name through a seed map, never by literal UID.
Nothing sleeps; the fake is driven explicitly.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.imap import apply as applier
from mailmind.imap import sync
from mailmind.imap.capabilities import probe_account
from mailmind.suggest import model as suggest
from tests.corpus import CORPUS


def _bundle(scope, world, names, operation=m.Operation.move, target="Archive"):
    return suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=operation,
        message_ids=[world["seed"][n] for n in names],
        target_container_id=world["containers"][target].id if target else None,
        summary="Newsletters and one-offs cluttering the inbox",
        reason="These have not been replied to and are not addressed to me personally",
    )


# ------------------------------------------------------------------- syncing


def test_sync_caches_every_message_with_its_placement(scope, world):
    count = scope.scalar(sa.select(sa.func.count()).select_from(m.Message))
    assert count == len(CORPUS)
    live = scope.scalar(
        sa.select(sa.func.count()).select_from(m.Placement).where(m.Placement.gone_at.is_(None))
    )
    assert live == len(CORPUS)


def _codes(scope, message_id):
    return set(
        scope.scalars(
            sa.select(m.Finding.code)
            .join(m.Assessment, m.Finding.assessment_id == m.Assessment.id)
            .where(m.Assessment.subject_id == message_id)
        )
    )


def test_mechanical_findings_are_recorded_without_a_model(scope, world):
    """A sync fetches headers only, so only header-level findings exist yet."""
    codes = _codes(scope, world["seed"]["spoofed_display_name"])
    assert "display_name_spoofs_address" in codes
    assert all(
        f.finding_class is m.FindingClass.mechanical
        for f in scope.scalars(sa.select(m.Finding))
    )


def test_body_findings_appear_once_a_body_is_actually_fetched(scope, world, backend):
    message_id = world["seed"]["spoofed_display_name"]
    assert "body_only_address" not in _codes(scope, message_id)

    placement = scope.scalar(sa.select(m.Placement).where(m.Placement.message_id == message_id))
    sync.fetch_and_cache_body(
        scope,
        world["account"],
        world["containers"]["INBOX"],
        placement,
        backend,
        budget_bytes=10_000_000,
    )
    assert "body_only_address" in _codes(scope, message_id)


def test_a_link_whose_text_disagrees_with_its_target_is_found(scope, world, backend):
    message_id = world["seed"]["instruction_shaped"]
    placement = scope.scalar(sa.select(m.Placement).where(m.Placement.message_id == message_id))
    sync.fetch_and_cache_body(
        scope,
        world["account"],
        world["containers"]["INBOX"],
        placement,
        backend,
        budget_bytes=10_000_000,
    )
    assert "link_target_mismatch" in _codes(scope, message_id)


def test_a_sender_never_seen_before_is_marked_and_a_familiar_one_is_not(
    scope, world, backend
):
    """The finding says what was known when the message arrived.

    It was unreachable: the row is written and flushed before the assessment runs, so
    counting messages from the address counted the message being assessed and every
    sender looked familiar.
    """
    assert "first_contact" in _codes(scope, world["seed"]["ordinary"])

    second = backend.add_message(
        "INBOX",
        b"From: Alice <alice@example.com>\r\n"
        b"To: me@example.org\r\n"
        b"Subject: One more thing\r\n"
        b"Date: Mon, 24 Aug 2026 09:00:00 +0000\r\n"
        b"Message-ID: <second@example.com>\r\n"
        b"\r\n"
        b"About Thursday.\r\n",
    )
    sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    scope.flush()

    placement = scope.scalar(
        sa.select(m.Placement).where(
            m.Placement.container_id == world["containers"]["INBOX"].id,
            m.Placement.uid == second,
        )
    )
    assert "first_contact" not in _codes(scope, placement.message_id)
    assert "first_contact" in _codes(scope, world["seed"]["ordinary"]), (
        "the first message stopped being first contact once a second one arrived"
    )


def test_a_cached_message_carries_the_size_the_server_reported(scope, world, backend):
    """Not the size of the blob that was parsed, which during a sync is headers only."""
    message = scope.get(m.Message, world["seed"]["ordinary"])
    raw = CORPUS["ordinary"]
    assert message.size_bytes == len(raw)
    assert message.size_bytes > len(raw.partition(b"\r\n\r\n")[0])


def test_a_message_with_no_message_id_is_still_cached_and_flagged(scope, world):
    codes = set(
        scope.scalars(
            sa.select(m.Finding.code)
            .join(m.Assessment, m.Finding.assessment_id == m.Assessment.id)
            .where(m.Assessment.subject_id == world["seed"]["no_message_id"])
        )
    )
    assert "no_message_id" in codes


def test_malformed_mime_is_marked_not_treated_as_empty(scope, world):
    message = scope.get(m.Message, world["seed"]["malformed_mime"])
    assert message.parse_status is not m.ParseStatus.ok


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Latin-1 bytes were never UTF-8, so the character is gone and U+FFFD is honest.
        ("eight_bit_display_name", "H\ufffdndler"),
        ("unknown_8bit_word", "H\ufffdndler"),
    ],
)
def test_eight_bit_bytes_in_a_display_name_are_cached_rather_than_fatal(
    scope, world, name, expected
):
    """The whole sync used to die here, on the folder rather than on the message.

    `email.headerregistry` decodes 8-bit bytes in address headers with surrogateescape,
    SQLite refuses to store a lone surrogate, and a mailbox with 187 folders synced none of
    them because the first one held a shop's name in Latin-1.
    """
    message = scope.get(m.Message, world["seed"][name])
    assert message.from_display == expected
    message.from_display.encode("utf-8")  # what the exception was raised by


def test_utf_8_in_a_domain_survives_as_the_character_it_was(scope, world):
    """The case seen in the wild, and the reason this replaces rather than drops.

    Those bytes *were* UTF-8 — one surrogate per byte of a two-byte character — so putting
    them back through utf-8 recovers the address somebody actually wrote.
    """
    message = scope.get(m.Message, world["seed"]["non_ascii_domain"])
    assert message.from_address == "hallo@gr\u00fc\u00dfe.example"
    assert message.from_display == "Gr\u00fc\u00dfe"


def test_an_incremental_sync_sees_an_out_of_band_flag_change(scope, world, backend):
    backend.out_of_band_mutate_flags("INBOX", world["uids"]["ordinary"], (r"\Seen",))
    report = sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    assert not report.full
    placement = scope.scalar(
        sa.select(m.Placement).where(m.Placement.message_id == world["seed"]["ordinary"])
    )
    assert placement.flags == r"\Seen"


# ------------------------------------------------------- proposing and review


def test_a_bundle_captures_a_premise_for_every_item(scope, world):
    bundle = _bundle(scope, world, ["newsletter", "instruction_shaped"])
    assert len(bundle.suggestions) == 2
    assert all(s.premise_modseq is not None for s in bundle.suggestions)
    assert all(s.premise_container_generation == 1 for s in bundle.suggestions)


def test_a_bundle_larger_than_the_limit_is_refused(scope, world):
    with pytest.raises(suggest.ProposalRefused, match="exceeds"):
        suggest.propose_bundle(
            scope,
            producer=world["producer"],
            account=world["account"],
            operation=m.Operation.move,
            message_ids=list(world["seed"].values()),
            target_container_id=world["containers"]["Archive"].id,
            summary="s",
            reason="r",
            max_size=2,
        )


def test_accepting_applies_and_the_mailbox_actually_changes(scope, world, backend):
    bundle = _bundle(scope, world, ["newsletter"])
    suggest.accept(scope, bundle, world["reviewer"])
    attempts = applier.apply_bundle(scope, bundle, backend)
    scope.commit()

    assert [a.outcome for a in attempts] == [m.ApplyOutcome.applied]
    assert bundle.status is m.BundleStatus.applied
    assert world["uids"]["newsletter"] not in backend.folders["INBOX"].messages
    assert len(backend.folders["Archive"].messages) == 1


def test_a_move_reports_best_effort_because_move_has_no_unchangedsince(scope, world, backend):
    bundle = _bundle(scope, world, ["newsletter"])
    suggest.accept(scope, bundle, world["reviewer"])
    attempt = applier.apply_bundle(scope, bundle, backend)[0]
    assert attempt.precondition is m.Precondition.best_effort
    assert attempt.guarantee_obtained is m.Precondition.best_effort


def test_a_flag_change_reports_the_conditional_guarantee_it_asked_for(scope, world, backend):
    bundle = suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=m.Operation.add_flag,
        flag=r"\Seen",
        message_ids=[world["seed"]["newsletter"]],
        summary="mark read",
        reason="already read elsewhere",
    )
    suggest.accept(scope, bundle, world["reviewer"])
    attempt = applier.apply_bundle(scope, bundle, backend)[0]
    assert attempt.outcome is m.ApplyOutcome.applied
    assert attempt.guarantee_obtained is m.Precondition.conditional


# ------------------------------------------------------------ the two gaps


def test_gap_one_something_that_moved_before_review_cannot_be_accepted(scope, world, backend):
    """Proposed, then the person filed it themselves in their own client."""
    bundle = _bundle(scope, world, ["newsletter", "ordinary"])
    backend.out_of_band_move("INBOX", world["uids"]["ordinary"], "Archive")
    sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)

    with pytest.raises(suggest.ProposalRefused, match="moved since this was proposed"):
        suggest.accept(scope, bundle, world["reviewer"])

    dead = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.stale]
    assert len(dead) == 1
    assert "has left INBOX" in dead[0].stale_detail


def test_the_reviewer_can_exclude_what_moved_and_accept_the_rest(scope, world, backend):
    bundle = _bundle(scope, world, ["newsletter", "ordinary"])
    backend.out_of_band_move("INBOX", world["uids"]["ordinary"], "Archive")
    sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    with pytest.raises(suggest.ProposalRefused):
        suggest.accept(scope, bundle, world["reviewer"])

    # The reviewer has now been shown what moved and says so.  The stale item is not
    # accepted by this; it stays dead.
    accepted = suggest.accept(scope, bundle, world["reviewer"], acknowledge_stale=True)
    assert len(accepted) == 1
    attempts = applier.apply_bundle(scope, bundle, backend)
    assert [a.outcome for a in attempts] == [m.ApplyOutcome.applied]
    stale = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.stale]
    assert len(stale) == 1


def test_gap_two_something_that_moves_after_acceptance_is_not_applied(scope, world, backend):
    """The dangerous gap: a person has already said yes."""
    bundle = _bundle(scope, world, ["newsletter"])
    suggest.accept(scope, bundle, world["reviewer"])

    # Between the yes and the doing, another client touches it.
    backend.out_of_band_mutate_flags("INBOX", world["uids"]["newsletter"], (r"\Flagged",))
    sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)

    attempts = applier.apply_bundle(scope, bundle, backend)
    assert [a.outcome for a in attempts] == [m.ApplyOutcome.refused_stale]
    assert bundle.suggestions[0].status is m.SuggestionStatus.stale
    # And the mailbox is untouched.
    assert world["uids"]["newsletter"] in backend.folders["INBOX"].messages


def test_gap_two_holds_even_when_the_cache_has_not_noticed(scope, world, backend):
    """No sync between the change and the apply: the server still refuses."""
    bundle = suggest.propose_bundle(
        scope,
        producer=world["producer"],
        account=world["account"],
        operation=m.Operation.add_flag,
        flag=r"\Seen",
        message_ids=[world["seed"]["newsletter"]],
        summary="mark read",
        reason="r",
    )
    suggest.accept(scope, bundle, world["reviewer"])
    backend.out_of_band_mutate_flags("INBOX", world["uids"]["newsletter"], (r"\Flagged",))

    attempts = applier.apply_bundle(scope, bundle, backend)
    assert [a.outcome for a in attempts] == [m.ApplyOutcome.refused_stale]


def test_a_bundle_that_lost_every_item_is_not_reported_as_applied(scope, world, backend):
    """Zero of zero used to count as all of them.

    ``applied == len(attempts)`` holds trivially when nothing was attempted, so a bundle
    whose every item died between acceptance and application announced itself as applied
    and the queue showed a change the mailbox never saw.
    """
    from mailmind.suggest import staleness

    bundle = _bundle(scope, world, ["newsletter"])
    suggest.accept(scope, bundle, world["reviewer"])

    # Somebody files it in their own client, and the next sync notices.
    backend.out_of_band_move("INBOX", world["uids"]["newsletter"], "Archive")
    sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    staleness.refresh_bundle(scope, bundle)
    assert all(s.status is m.SuggestionStatus.stale for s in bundle.suggestions)

    with pytest.raises(applier.NotApplicable) as refused:
        applier.apply_bundle(scope, bundle, backend)
    assert "nothing was done to the mailbox" in str(refused.value)
    assert bundle.status is m.BundleStatus.accepted


def test_a_recreated_folder_kills_everything_resting_on_it(scope, world, backend):
    bundle = _bundle(scope, world, ["newsletter", "ordinary"])
    backend.force_uidvalidity_change("INBOX")
    report = sync.sync_container(scope, world["account"], world["containers"]["INBOX"], backend)
    assert report.identity_broken
    assert report.suggestions_killed == 2
    assert world["containers"]["INBOX"].generation == 2
    assert all(s.status is m.SuggestionStatus.stale for s in bundle.suggestions)
    assert "recreated" in bundle.suggestions[0].stale_detail


# -------------------------------------------------------------- health, expiry


def test_nothing_is_applied_against_an_unhealthy_account(scope, world, backend):
    bundle = _bundle(scope, world, ["newsletter"])
    suggest.accept(scope, bundle, world["reviewer"])
    world["account"].health = m.AccountHealth.read_only
    with pytest.raises(applier.NotApplicable, match="not healthy"):
        applier.apply_bundle(scope, bundle, backend)


def test_a_missing_declared_capability_is_loud(scope, world, backend):
    backend.set_capabilities(frozenset({"MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE"}))
    report = probe_account(scope, world["account"], backend)
    assert report.missing == ("CONDSTORE",)
    assert world["account"].health is m.AccountHealth.read_only
    assert "CONDSTORE" in world["account"].health_detail


def test_unreviewed_bundles_expire(scope, world):
    bundle = _bundle(scope, world, ["newsletter"])
    bundle.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    assert suggest.expire_due(scope) == 1
    assert bundle.status is m.BundleStatus.expired


def test_the_record_keeps_who_accepted_what(scope, world, backend):
    bundle = _bundle(scope, world, ["newsletter"])
    suggest.accept(scope, bundle, world["reviewer"])
    applier.apply_bundle(scope, bundle, backend)
    verbs = list(scope.scalars(sa.select(m.AuditEvent.verb).order_by(m.AuditEvent.seq)))
    assert "bundle_proposed" in verbs and "bundle_accepted" in verbs
    accepted = scope.scalar(
        sa.select(m.AuditEvent).where(m.AuditEvent.verb == "bundle_accepted")
    )
    assert accepted.actor_id == world["reviewer"].id
    assert accepted.actor_kind == "person"


def test_a_producer_cannot_withdraw_someone_elses_bundle(scope, world):
    bundle = _bundle(scope, world, ["newsletter"])
    other = scope.add(m.Producer(kind=m.ProducerKind.agent, name="other"))
    scope.flush()
    with pytest.raises(suggest.ProposalRefused, match="only be withdrawn"):
        suggest.withdraw(scope, bundle, other, "mine now")


def test_a_message_leaving_is_noticed_even_on_a_server_offering_qresync(scope, world, backend):
    """QRESYNC is declared by real servers and not implemented here.

    A sync that trusted the capability instead of the implementation stopped noticing
    removals entirely, and every suggestion resting on a moved message stayed fresh.
    """
    assert "QRESYNC" not in backend.capabilities()
    backend.set_capabilities(backend.capabilities() | {"QRESYNC"})

    bundle = _bundle(scope, world, ["newsletter"])
    backend.out_of_band_move("INBOX", world["uids"]["newsletter"], "Archive")

    report = sync.sync_container(
        scope, world["account"], world["containers"]["INBOX"], backend
    )
    assert not report.full, "this must hold on the incremental path, not only a full sync"
    assert report.vanished == 1

    with pytest.raises(suggest.ProposalRefused):
        suggest.accept(scope, bundle, world["reviewer"])
    assert bundle.suggestions[0].status is m.SuggestionStatus.stale
