"""Making, deciding and expiring bundles.

A bundle is the reviewed unit: one operation, one target, an enumerated list of messages.
Homogeneity is what makes showing the whole effect possible, and showing the whole effect
is what keeps accepting it from being a bulk accept over things nobody looked at.

Whether that holds up under a mailbox with thousands of messages in it is the open
question this iteration exists to answer.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.imap.sync import flags_hash
from mailmind.suggest import staleness


class ProposalRefused(Exception):
    """The bundle could not be built as described."""


NEEDS_TARGET = {m.Operation.move}
NEEDS_FLAG = {m.Operation.add_flag, m.Operation.remove_flag}


def propose_bundle(
    scope: TenantScope,
    *,
    producer: m.Producer,
    account: m.Account,
    operation: m.Operation,
    message_ids: list[int],
    summary: str,
    reason: str,
    target_container_id: int | None = None,
    flag: str | None = None,
    expiry_days: int = 7,
    max_size: int = 500,
) -> m.Bundle:
    if not message_ids:
        raise ProposalRefused("a bundle with no messages would have no effect to review")
    if len(message_ids) > max_size:
        raise ProposalRefused(
            f"{len(message_ids)} messages exceeds the {max_size} a single bundle may hold; "
            "propose narrower bundles so their effect can be read"
        )
    if operation in NEEDS_TARGET and target_container_id is None:
        raise ProposalRefused(f"{operation.value} needs a target container")
    if operation in NEEDS_FLAG and not flag:
        raise ProposalRefused(f"{operation.value} needs a flag")

    if target_container_id is not None:
        target = scope.get(m.Container, target_container_id)
        if target is None or target.account_id != account.id:
            raise ProposalRefused("the target container is not part of this account")

    if account.health is m.AccountHealth.down:
        raise ProposalRefused(
            f"account {account.name} is not reachable, so nothing can be proposed against it"
        )

    bundle = m.Bundle(
        account_id=account.id,
        producer_id=producer.id,
        action_kind=m.ActionKind.state,
        operation=operation,
        target_container_id=target_container_id,
        flag=flag,
        summary=summary,
        reason=reason,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=expiry_days),
    )
    scope.add(bundle)
    scope.flush()

    seen: set[int] = set()
    for message_id in message_ids:
        if message_id in seen:
            continue
        seen.add(message_id)
        placement = _live_placement(scope, account, message_id)
        if placement is None:
            raise ProposalRefused(
                f"message {message_id} is not currently in any container of this account"
            )
        if placement.container_id == target_container_id:
            raise ProposalRefused(f"message {message_id} is already in the target container")
        container = scope.get(m.Container, placement.container_id)
        scope.add(
            m.Suggestion(
                bundle_id=bundle.id,
                message_id=message_id,
                source_container_id=placement.container_id,
                premise_container_generation=container.generation,
                premise_uid=placement.uid,
                premise_modseq=placement.modseq,
                premise_flags_hash=flags_hash(placement.flags),
            )
        )
    scope.flush()
    scope.audit(
        "bundle_proposed",
        actor_kind="producer",
        actor_id=producer.id,
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={
            "operation": operation.value,
            "messages": len(seen),
            "target_container_id": target_container_id,
        },
    )
    return bundle


def _live_placement(
    scope: TenantScope, account: m.Account, message_id: int
) -> m.Placement | None:
    return scope.scalar(
        sa.select(m.Placement)
        .join(m.Container, m.Placement.container_id == m.Container.id)
        .where(
            m.Placement.message_id == message_id,
            m.Placement.gone_at.is_(None),
            m.Container.account_id == account.id,
            m.Placement.container_generation == m.Container.generation,
        )
        .order_by(m.Placement.seen_at.desc())
    )


def exclude(scope: TenantScope, suggestion: m.Suggestion, reviewer: m.Producer) -> None:
    """Drop one item before accepting the rest.  Re-scoping, not rejecting."""
    if suggestion.status is not m.SuggestionStatus.proposed:
        raise ProposalRefused("only a proposed item can be excluded")
    suggestion.status = m.SuggestionStatus.excluded
    scope.audit(
        "suggestion_excluded",
        actor_kind="person",
        actor_id=reviewer.id,
        subject_kind="suggestion",
        subject_id=suggestion.id,
    )


def accept(
    scope: TenantScope,
    bundle: m.Bundle,
    reviewer: m.Producer,
    *,
    acknowledge_stale: bool = False,
) -> list[m.Suggestion]:
    """The only transition that matters.

    Staleness is checked first, and a bundle holding something that moved cannot simply
    be accepted: the reviewer is told what changed and has to say they have seen it.
    ``acknowledge_stale`` is that second, deliberate act.  It never accepts the stale
    items — they stay dead — it only says the person read what happened to them.

    Without this the reviewer would be accepting around a change they were never shown,
    which is the failure this service exists to prevent.
    """
    if bundle.status is not m.BundleStatus.proposed:
        raise ProposalRefused(f"this bundle is {bundle.status.value}, not awaiting review")

    staleness.refresh_bundle(scope, bundle)
    if bundle.status is m.BundleStatus.stale:
        # Everything it referred to moved on, so refreshing closed it just now.  Say that,
        # rather than the older "every item has died", which read as a refusal to act on a
        # bundle still sitting in the queue and left no way to clear it.
        raise ProposalRefused(
            "every message in this bundle moved on before it was accepted, so there is "
            "nothing left to apply; it has been closed rather than rejected, because "
            "nobody turned it down"
        )
    stale = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.stale]
    if stale and not acknowledge_stale:
        raise ProposalRefused(
            f"{len(stale)} of these messages moved since this was proposed; "
            "review what changed and acknowledge it before accepting the rest"
        )
    if stale:
        scope.audit(
            "stale_acknowledged",
            actor_kind="person",
            actor_id=reviewer.id,
            subject_kind="bundle",
            subject_id=bundle.id,
            payload={"items": [s.id for s in stale]},
        )

    accepted = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.proposed]
    if not accepted:
        raise ProposalRefused("every item in this bundle has been excluded or has died")

    for suggestion in accepted:
        suggestion.status = m.SuggestionStatus.accepted

    bundle.status = m.BundleStatus.accepted
    bundle.decided_at = dt.datetime.now(dt.UTC)
    bundle.decided_by_id = reviewer.id
    scope.audit(
        "bundle_accepted",
        actor_kind="person",
        actor_id=reviewer.id,
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"items": len(accepted)},
    )
    return accepted


def reject(
    scope: TenantScope, bundle: m.Bundle, reviewer: m.Producer, reason: str | None = None
) -> None:
    if bundle.status is not m.BundleStatus.proposed:
        raise ProposalRefused(f"this bundle is {bundle.status.value}, not awaiting review")
    bundle.status = m.BundleStatus.rejected
    bundle.decided_at = dt.datetime.now(dt.UTC)
    bundle.decided_by_id = reviewer.id
    bundle.decision_reason = reason
    for suggestion in bundle.suggestions:
        if suggestion.status is m.SuggestionStatus.proposed:
            suggestion.status = m.SuggestionStatus.rejected
    scope.audit(
        "bundle_rejected",
        actor_kind="person",
        actor_id=reviewer.id,
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"reason": reason},
    )


def withdraw(scope: TenantScope, bundle: m.Bundle, producer: m.Producer, reason: str) -> None:
    """A producer taking back its own suggestion.  Never somebody else's."""
    if bundle.producer_id != producer.id:
        raise ProposalRefused("a bundle can only be withdrawn by the producer that made it")
    if bundle.status is not m.BundleStatus.proposed:
        raise ProposalRefused(f"this bundle is {bundle.status.value} and cannot be withdrawn")
    bundle.status = m.BundleStatus.withdrawn
    bundle.decision_reason = reason
    for suggestion in bundle.suggestions:
        if suggestion.status is m.SuggestionStatus.proposed:
            suggestion.status = m.SuggestionStatus.withdrawn
    scope.audit(
        "bundle_withdrawn",
        actor_kind="producer",
        actor_id=producer.id,
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"reason": reason},
    )


def expire_due(scope: TenantScope, now: dt.datetime | None = None) -> int:
    """Suggestions nobody gets to expire rather than accumulate forever."""
    now = now or dt.datetime.now(dt.UTC)
    due = scope.scalars(
        sa.select(m.Bundle).where(
            m.Bundle.status == m.BundleStatus.proposed, m.Bundle.expires_at <= now
        )
    ).all()
    for bundle in due:
        bundle.status = m.BundleStatus.expired
        for suggestion in bundle.suggestions:
            if suggestion.status is m.SuggestionStatus.proposed:
                suggestion.status = m.SuggestionStatus.expired
        scope.audit(
            "bundle_expired",
            actor_kind="service",
            subject_kind="bundle",
            subject_id=bundle.id,
        )
    return len(due)
