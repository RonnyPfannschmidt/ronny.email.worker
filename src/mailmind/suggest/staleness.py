"""Did what this suggestion assumed stop being true?

One function, called twice: before a bundle is shown, and again per item immediately
before it is applied.  Both gaps are real — proposing to reviewing, and reviewing to
applying — and the second is the dangerous one, because a person has already said yes.
"""

from __future__ import annotations

import attrs
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.imap.sync import flags_hash


@attrs.frozen
class Freshness:
    fresh: bool
    #: What changed, in words a reviewer can act on.  A dead suggestion tells the person
    #: what moved rather than simply disappearing.
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.fresh


FRESH = Freshness(True)


def check(scope: TenantScope, suggestion: m.Suggestion) -> Freshness:
    container = scope.get(m.Container, suggestion.source_container_id)
    if container is None:
        return Freshness(False, "the folder this referred to is no longer known")

    if suggestion.message_id is None:
        return _check_container(scope, container, suggestion)

    if container.generation != suggestion.premise_container_generation:
        return Freshness(
            False,
            f"{container.name} was recreated since this was proposed, so the message it "
            "referred to can no longer be identified",
        )

    placement = scope.scalar(
        sa.select(m.Placement).where(
            m.Placement.container_id == container.id,
            m.Placement.container_generation == suggestion.premise_container_generation,
            m.Placement.uid == suggestion.premise_uid,
        )
    )
    if placement is None or placement.gone_at is not None:
        return Freshness(False, f"the message has left {container.name}")

    if suggestion.premise_modseq is not None:
        if placement.modseq != suggestion.premise_modseq:
            return Freshness(
                False,
                f"the message changed in {container.name} after this was proposed "
                f"(flags are now: {placement.flags or 'none'})",
            )
        return FRESH

    # No CONDSTORE on this account, so the premise is the weaker one: flags and nothing
    # else.  Changes this cannot see are exactly why the operation will report only a
    # best-effort guarantee when it applies.
    if flags_hash(placement.flags) != suggestion.premise_flags_hash:
        return Freshness(
            False,
            f"the message's flags changed in {container.name} after this was proposed "
            f"(now: {placement.flags or 'none'})",
        )
    return FRESH


def _check_container(
    scope: TenantScope, container: m.Container, suggestion: m.Suggestion
) -> Freshness:
    """Is the folder still one there is any point discarding?

    The premise a discard rests on is that the folder holds nothing.  Mail arriving in it
    is exactly the change a person would want to hear about before the folder went away
    with it — and it is the ordinary case, because a folder that looks abandoned is
    precisely the kind a forgotten filter still delivers into.
    """
    if container.discarded_at is not None:
        return Freshness(False, f"{container.name} is already gone")

    if container.generation != suggestion.premise_container_generation:
        # Deleted and remade under the same name while this waited.  Somebody wanted it,
        # and it is not the folder that was proposed for removal.
        return Freshness(
            False,
            f"{container.name} was recreated since this was proposed, so it is not the "
            "folder this offered to remove",
        )

    held = live_message_count(scope, container)
    if held:
        return Freshness(
            False,
            f"{container.name} is no longer empty — {held} messages arrived in it after "
            "this was proposed",
        )
    return FRESH


def live_message_count(scope: TenantScope, container: m.Container) -> int:
    """How much the cache believes this folder holds.

    The cache, not the server: this is a premise, and a premise is what was believed when
    the proposal was made.  The server is asked again at the moment of applying, which is
    the check that actually protects anything.
    """
    return (
        scope.scalar(
            sa.select(sa.func.count())
            .select_from(m.Placement)
            .where(
                m.Placement.container_id == container.id,
                m.Placement.container_generation == container.generation,
                m.Placement.gone_at.is_(None),
            )
        )
        or 0
    )


def refresh_bundle(scope: TenantScope, bundle: m.Bundle) -> dict[int, Freshness]:
    """Check every live item and mark the ones that have died.

    Called before the bundle is shown.  Items that have gone stale are marked, not
    hidden: 03 says a reviewer is told what changed.

    A bundle whose every item dies this way dies with them.  It cannot be accepted —
    there is nothing left to apply — and it must not have to be *rejected*, because
    rejecting is a person saying no and no person said anything: the mail moved on by
    itself.  So it goes to :attr:`~mailmind.db.models.BundleStatus.stale` here, which
    takes it out of the queue and leaves a record of what actually happened.
    """
    verdicts: dict[int, Freshness] = {}
    for suggestion in bundle.suggestions:
        if suggestion.status not in (
            m.SuggestionStatus.proposed,
            m.SuggestionStatus.accepted,
        ):
            continue
        verdict = check(scope, suggestion)
        verdicts[suggestion.id] = verdict
        if not verdict.fresh:
            suggestion.status = m.SuggestionStatus.stale
            suggestion.stale_detail = verdict.detail
            scope.audit(
                "suggestion_stale",
                actor_kind="service",
                subject_kind="suggestion",
                subject_id=suggestion.id,
                payload={"detail": verdict.detail},
            )

    _close_if_nothing_survived(scope, bundle)
    return verdicts


def _close_if_nothing_survived(scope: TenantScope, bundle: m.Bundle) -> None:
    """Take a bundle out of the queue once staleness has emptied it.

    Only when something actually went stale.  A bundle the reviewer emptied by excluding
    every item is a different story with a person in it, and is not this.
    """
    if bundle.status is not m.BundleStatus.proposed:
        return
    live = [
        s
        for s in bundle.suggestions
        if s.status in (m.SuggestionStatus.proposed, m.SuggestionStatus.accepted)
    ]
    died = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.stale]
    if live or not died:
        return

    bundle.status = m.BundleStatus.stale
    scope.audit(
        "bundle_stale",
        actor_kind="service",
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"items": len(died)},
    )


def sweep_queue(scope: TenantScope, account_ids: set[int] | None = None) -> int:
    """Refresh every bundle still awaiting review, and return how many closed.

    Drawing the queue is the moment somebody is about to act on it, so it is also the
    moment a bundle that quietly died should stop being offered.  Without this a bundle
    whose messages all moved keeps its place in the list until somebody opens it, which is
    exactly the click this is meant to save them.

    The work is one freshness check per live item of each proposed bundle.  A review queue
    is a human queue — a handful of bundles somebody is going to read — so this is bounded
    by what a person can get through, not by the size of the mailbox.
    """
    stmt = sa.select(m.Bundle).where(m.Bundle.status == m.BundleStatus.proposed)
    if account_ids is not None:
        stmt = stmt.where(m.Bundle.account_id.in_(account_ids))
    closed = 0
    for bundle in scope.scalars(stmt):
        refresh_bundle(scope, bundle)
        if bundle.status is m.BundleStatus.stale:
            closed += 1
    return closed
