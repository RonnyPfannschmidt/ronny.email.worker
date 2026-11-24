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


async def check(scope: TenantScope, suggestion: m.Suggestion) -> Freshness:
    container = await scope.get(m.Container, suggestion.source_container_id)
    if container is None:
        return Freshness(False, "the folder this referred to is no longer known")

    if suggestion.message_id is None:
        return await _check_container(scope, container, suggestion)

    # Columns, not the row: loading a placement brings its message, its addresses, its
    # folder and the account along, once per item.
    placement = (
        await scope.execute(
            sa.select(m.Placement.gone_at, m.Placement.modseq, m.Placement.flags).where(
                m.Placement.container_id == container.id,
                m.Placement.container_generation == suggestion.premise_container_generation,
                m.Placement.uid == suggestion.premise_uid,
            )
        )
    ).one_or_none()
    return _check_message(
        Premise(
            generation=suggestion.premise_container_generation,
            modseq=suggestion.premise_modseq,
            flags_hash=suggestion.premise_flags_hash,
        ),
        container_name=container.name,
        container_generation=container.generation,
        # A row that is gone and a row that never was are the same answer.
        present=placement is not None and placement.gone_at is None,
        modseq=placement.modseq if placement is not None else None,
        flags=placement.flags if placement is not None else "",
    )


@attrs.frozen
class Premise:
    """What a message item believed when it was proposed."""

    generation: int
    modseq: int | None
    flags_hash: str


def _check_message(
    premise: Premise,
    *,
    container_name: str,
    container_generation: int,
    present: bool,
    modseq: int | None,
    flags: str | None,
) -> Freshness:
    """The rule itself, on plain values, so one item and a whole queue share it."""
    if container_generation != premise.generation:
        return Freshness(
            False,
            f"{container_name} was recreated since this was proposed, so the message it "
            "referred to can no longer be identified",
        )

    if not present:
        return Freshness(False, f"the message has left {container_name}")

    if premise.modseq is not None:
        if modseq != premise.modseq:
            return Freshness(
                False,
                f"the message changed in {container_name} after this was proposed "
                f"(flags are now: {flags or 'none'})",
            )
        return FRESH

    # No CONDSTORE on this account, so the premise is the weaker one: flags and nothing
    # else.  Changes this cannot see are exactly why the operation will report only a
    # best-effort guarantee when it applies.
    if flags_hash(flags or "") != premise.flags_hash:
        return Freshness(
            False,
            f"the message's flags changed in {container_name} after this was proposed "
            f"(now: {flags or 'none'})",
        )
    return FRESH


async def _check_container(
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

    held = await live_message_count(scope, container)
    if held:
        return Freshness(
            False,
            f"{container.name} is no longer empty — {held} messages arrived in it after "
            "this was proposed",
        )
    return FRESH


async def live_message_count(scope: TenantScope, container: m.Container) -> int:
    """How much the cache believes this folder holds.

    The cache, not the server: this is a premise, and a premise is what was believed when
    the proposal was made.  The server is asked again at the moment of applying, which is
    the check that actually protects anything.
    """
    return (
        await scope.scalar(
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


async def refresh_bundle(scope: TenantScope, bundle: m.Bundle) -> dict[int, Freshness]:
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
        verdict = await check(scope, suggestion)
        verdicts[suggestion.id] = verdict
        if not verdict.fresh:
            suggestion.status = m.SuggestionStatus.stale
            suggestion.stale_detail = verdict.detail
            await scope.audit(
                "suggestion_stale",
                actor_kind="service",
                subject_kind="suggestion",
                subject_id=suggestion.id,
                payload={"detail": verdict.detail},
            )

    await _close_if_nothing_survived(scope, bundle)
    return verdicts


async def _close_if_nothing_survived(scope: TenantScope, bundle: m.Bundle) -> None:
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
    await scope.audit(
        "bundle_stale",
        actor_kind="service",
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"items": len(died)},
    )


async def _suspect_bundles(scope: TenantScope, account_ids: set[int] | None) -> set[int]:
    """Proposed bundles that are not plainly intact.

    Intact means at least one live item, all of them message items and all of them fresh.
    A folder item asks a different question, and a bundle with nothing live may only be
    waiting to be closed: a sync can kill the items without closing the bundle.
    """
    proposed = sa.select(m.Bundle.id).where(m.Bundle.status == m.BundleStatus.proposed)
    if account_ids is not None:
        proposed = proposed.where(m.Bundle.account_id.in_(account_ids))

    items = (
        sa.select(
            m.Suggestion.bundle_id,
            m.Suggestion.message_id,
            m.Suggestion.premise_container_generation,
            m.Suggestion.premise_modseq,
            m.Suggestion.premise_flags_hash,
            m.Container.name,
            m.Container.generation,
            m.Placement.id.label("placement_id"),
            m.Placement.gone_at,
            m.Placement.modseq,
            m.Placement.flags,
        )
        .join(m.Container, m.Container.id == m.Suggestion.source_container_id)
        .outerjoin(
            m.Placement,
            sa.and_(
                m.Placement.container_id == m.Suggestion.source_container_id,
                m.Placement.container_generation == m.Suggestion.premise_container_generation,
                m.Placement.uid == m.Suggestion.premise_uid,
            ),
        )
        .where(
            m.Suggestion.bundle_id.in_(proposed),
            m.Suggestion.status.in_((m.SuggestionStatus.proposed, m.SuggestionStatus.accepted)),
        )
    )
    alive: set[int] = set()
    dying: set[int] = set()
    for row in await scope.execute(items):
        alive.add(row.bundle_id)
        if row.message_id is None or not _check_message(
            Premise(
                generation=row.premise_container_generation,
                modseq=row.premise_modseq,
                flags_hash=row.premise_flags_hash,
            ),
            container_name=row.name,
            container_generation=row.generation,
            present=row.placement_id is not None and row.gone_at is None,
            modseq=row.modseq,
            flags=row.flags,
        ):
            dying.add(row.bundle_id)
    return (set(await scope.all(proposed)) - alive) | dying


async def sweep_queue(
    scope: TenantScope,
    account_ids: set[int] | None = None,
    checkpoint=None,  # noqa: ANN001 - async () -> None, usually scope.commit
) -> int:
    """Refresh every bundle still awaiting review, and return how many closed.

    The runner does this on its tick.  Without it a bundle whose messages all moved keeps
    its place in the list until somebody opens it, which is exactly the click this is
    meant to save them.

    Only bundles that may have died are loaded.  A few dozen bundles of a few dozen items
    each is an ordinary queue, and checking those one item at a time took twelve seconds.
    """
    closed = 0
    suspects = await _suspect_bundles(scope, account_ids)
    if not suspects:
        return closed
    stmt = sa.select(m.Bundle).where(m.Bundle.id.in_(suspects)).order_by(m.Bundle.id)
    for bundle in await scope.all(stmt):
        await refresh_bundle(scope, bundle)
        if bundle.status is m.BundleStatus.stale:
            closed += 1
        if checkpoint is not None:
            await checkpoint()
    return closed
