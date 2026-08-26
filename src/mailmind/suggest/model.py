"""Making, deciding and expiring bundles.

A bundle is the reviewed unit: one operation, one target, an enumerated list of messages —
or, when the operation is one done to folders rather than to mail, an enumerated list of
folders.  Homogeneity is what makes showing the whole effect possible, and showing the
whole effect is what keeps accepting it from being a bulk accept over things nobody looked
at.  That argument never mentioned messages, which is why folders fit inside it.

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

#: Longest folder name that will be proposed.  RFC 3501 sets no limit and servers differ
#: wildly; this is short enough that every server met so far takes it and long enough that
#: no sensible hierarchy runs out of room.
MAX_CONTAINER_NAME = 255


def validate_container_name(name: str, delimiter: str | None) -> str:
    """Refuse a folder name here rather than letting the server refuse it later.

    Later is after a person has read a bundle and accepted it, which is the worst moment
    to discover the name was never going to work.  These are the refusals that hold on
    every server; the ones that do not — a namespace you may not write in, a character
    this particular server dislikes — still come back from CREATE, and are reported then.
    """
    if name != name.strip():
        raise ProposalRefused(
            f"the folder name {name!r} has whitespace at one end, which is a folder that "
            "looks like another one"
        )
    if not name:
        raise ProposalRefused("a folder needs a name")
    if len(name) > MAX_CONTAINER_NAME:
        raise ProposalRefused(
            f"the folder name is {len(name)} characters, more than the "
            f"{MAX_CONTAINER_NAME} that will be proposed"
        )
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise ProposalRefused(
            "the folder name holds characters that do not render, so nobody reviewing it "
            "could see what they were agreeing to"
        )
    if name.upper() == "INBOX":
        raise ProposalRefused("INBOX already exists and is not somewhere to be created")
    if delimiter:
        if name.startswith(delimiter) or name.endswith(delimiter):
            raise ProposalRefused(
                f"the folder name starts or ends with {delimiter!r}, which names a level "
                "of the hierarchy with nothing in it"
            )
        if delimiter * 2 in name:
            raise ProposalRefused(
                f"the folder name has an empty level in it ({delimiter * 2!r})"
            )
    return name


def _account_delimiter(scope: TenantScope, account: m.Account) -> str | None:
    """The hierarchy separator this account's server uses, as its folders report it."""
    return scope.scalar(
        sa.select(m.Container.delimiter)
        .where(
            m.Container.account_id == account.id,
            m.Container.delimiter.is_not(None),
        )
        .limit(1)
    )


def resolve_target(
    scope: TenantScope, account: m.Account, name: str
) -> tuple[m.Container, bool]:
    """The container a move should land in, made if it is not there yet.

    Made *locally*.  Nothing is asked of the server here: what this writes is a row
    saying a folder has been proposed, so the target is an ordinary container everywhere
    the bundle is read.  The server is only asked once a person has accepted the move,
    which is what makes accepting the move the thing that authorises the folder.

    Returns the container and whether it is one that does not exist yet, because the
    reviewer has to be told which.
    """
    delimiter = _account_delimiter(scope, account)
    validate_container_name(name, delimiter)

    existing = scope.scalar(
        sa.select(m.Container).where(
            m.Container.account_id == account.id, m.Container.name == name
        )
    )
    if existing is not None:
        if existing.discarded_at is not None:
            # We got rid of it and now something wants it back.  Reviving the row keeps
            # the unique name and whatever history hangs off it, rather than colliding.
            existing.discarded_at = None
            existing.exists_on_server = False
            existing.generation += 1
        return existing, not existing.exists_on_server

    container = m.Container(
        account_id=account.id,
        name=name,
        delimiter=delimiter,
        exists_on_server=False,
    )
    scope.add(container)
    scope.flush()
    return container, True


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
    target_container_name: str | None = None,
    flag: str | None = None,
    expiry_days: int = 7,
    max_size: int = 500,
) -> m.Bundle:
    if operation in m.CONTAINER_OPERATIONS:
        raise ProposalRefused(
            f"{operation.value} is an operation over folders, not messages; "
            "propose it as a discard"
        )
    if not message_ids:
        raise ProposalRefused("a bundle with no messages would have no effect to review")
    if len(message_ids) > max_size:
        raise ProposalRefused(
            f"{len(message_ids)} messages exceeds the {max_size} a single bundle may hold; "
            "propose narrower bundles so their effect can be read"
        )
    if target_container_id is not None and target_container_name is not None:
        raise ProposalRefused(
            "name the target folder or give its id, not both — they can disagree, and "
            "then nothing knows which one the bundle meant"
        )
    if operation in NEEDS_TARGET and target_container_id is None and not target_container_name:
        raise ProposalRefused(f"{operation.value} needs a target container")
    if operation in NEEDS_FLAG and not flag:
        raise ProposalRefused(f"{operation.value} needs a flag")

    if account.health is m.AccountHealth.down:
        raise ProposalRefused(
            f"account {account.name} is not reachable, so nothing can be proposed against it"
        )

    if target_container_name is not None:
        target, _ = resolve_target(scope, account, target_container_name)
        target_container_id = target.id
    elif target_container_id is not None:
        target = scope.get(m.Container, target_container_id)
        if target is None or target.account_id != account.id:
            raise ProposalRefused("the target container is not part of this account")
        if target.discarded_at is not None:
            raise ProposalRefused(
                f"{target.name} has been discarded; name it again to have it made afresh"
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
            "target_created": target_container_name is not None,
        },
    )
    return bundle


def propose_discard(
    scope: TenantScope,
    *,
    producer: m.Producer,
    account: m.Account,
    container_ids: list[int],
    summary: str,
    reason: str,
    expiry_days: int = 7,
    max_size: int = 500,
) -> m.Bundle:
    """Propose getting rid of folders that hold nothing.

    The same reviewed unit, with folders where the messages usually are: one operation
    over an enumerated list, and 03's argument holds unchanged — twelve empty folders is
    one decision shown twelve times, not twelve decisions dressed as one.

    Empty is the whole of why this is allowed at all.  01 says mail has no undo, and a
    folder holding nothing is the one removal with nothing to undo.  So emptiness is
    checked here, checked again against the server immediately before each folder goes,
    and recorded as the premise in between.
    """
    if not container_ids:
        raise ProposalRefused("a bundle with no folders would have no effect to review")
    if len(container_ids) > max_size:
        raise ProposalRefused(
            f"{len(container_ids)} folders exceeds the {max_size} a single bundle may hold; "
            "propose narrower bundles so their effect can be read"
        )
    if account.health is m.AccountHealth.down:
        raise ProposalRefused(
            f"account {account.name} is not reachable, so nothing can be proposed against it"
        )

    wanted: list[m.Container] = []
    seen: set[int] = set()
    for container_id in container_ids:
        if container_id in seen:
            continue
        seen.add(container_id)
        container = scope.get(m.Container, container_id)
        if container is None or container.account_id != account.id:
            raise ProposalRefused(f"folder {container_id} is not part of this account")
        wanted.append(container)

    going = {c.name for c in wanted}
    for container in wanted:
        _refuse_undiscardable(scope, account, container, going)

    bundle = m.Bundle(
        account_id=account.id,
        producer_id=producer.id,
        action_kind=m.ActionKind.state,
        operation=m.Operation.discard_container,
        summary=summary,
        reason=reason,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=expiry_days),
    )
    scope.add(bundle)
    scope.flush()

    for container in wanted:
        scope.add(
            m.Suggestion(
                bundle_id=bundle.id,
                source_container_id=container.id,
                premise_container_generation=container.generation,
                premise_message_count=0,
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
            "operation": m.Operation.discard_container.value,
            "folders": sorted(going),
        },
    )
    return bundle


def _refuse_undiscardable(
    scope: TenantScope, account: m.Account, container: m.Container, going: set[str]
) -> None:
    """Every reason a folder is not this service's to remove."""
    if container.discarded_at is not None:
        raise ProposalRefused(f"{container.name} has already been discarded")
    if container.name.upper() == "INBOX":
        raise ProposalRefused("INBOX is where mail arrives and is not somewhere to discard")
    if container.special_use:
        raise ProposalRefused(
            f"{container.name} is the account's {container.special_use} folder, which the "
            "mail client and this service both rely on being there"
        )

    held = staleness.live_message_count(scope, container)
    if held:
        raise ProposalRefused(
            f"{container.name} holds {held} messages; only a folder holding nothing is "
            "discarded, because that is the only removal that cannot lose mail"
        )

    children = _children_of(scope, account, container)
    outside = sorted(name for name in children if name not in going)
    if outside:
        raise ProposalRefused(
            f"{container.name} still has folders under it that this bundle does not "
            f"remove: {', '.join(outside)}"
        )


def _children_of(scope: TenantScope, account: m.Account, container: m.Container) -> list[str]:
    """The folders sitting under this one, by name.

    Hierarchy in IMAP is a naming convention rather than a structure, so this is a prefix
    match on the account's separator.  Without a separator the server has no hierarchy and
    nothing can be under anything.
    """
    if not container.delimiter:
        return []
    prefix = container.name + container.delimiter
    return list(
        scope.scalars(
            sa.select(m.Container.name).where(
                m.Container.account_id == account.id,
                m.Container.discarded_at.is_(None),
                m.Container.name.startswith(prefix, autoescape=True),
            )
        )
    )


#: How to tell a person an item of this bundle stopped holding.  Both are the same
#: machinery and it would read as machinery if both said "changed": what goes wrong with a
#: message is that it moved somewhere else, and what goes wrong with a folder is that mail
#: turned up in it.
STALE_WORDS = {m.Operation.discard_container: ("folder", "filled up")}
MESSAGE_WORDS = ("message", "moved")


def _items(bundle: m.Bundle) -> tuple[str, str]:
    return STALE_WORDS.get(bundle.operation, MESSAGE_WORDS)


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
        noun, verb = _items(bundle)
        raise ProposalRefused(
            f"every {noun} in this bundle {verb} before it was accepted, so there is "
            "nothing left to apply; it has been closed rather than rejected, because "
            "nobody turned it down"
        )
    stale = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.stale]
    if stale and not acknowledge_stale:
        noun, verb = _items(bundle)
        raise ProposalRefused(
            f"{len(stale)} of these {noun}s {verb} since this was proposed; "
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
