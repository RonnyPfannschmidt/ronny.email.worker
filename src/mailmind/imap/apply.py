"""The only place anything is written to a mailbox.

Note who can reach it: this is called by the review flow, never from the MCP surface.
The applier takes resolved operations and does not read mail; the producer reads mail and
cannot reach here.  No row of 02's table has two of the three.

Each operation declares how strong a guarantee it needs, and records what it actually
got.  Those differ for MOVE, and the difference is reported rather than glossed over.

Two of the operations here are about folders rather than mail: a move may have to make the
folder it lands in, and a discard removes one that holds nothing.  Both go through review
like everything else — this module is still reached only from the review flow, and there
is still no way to it from the agent surface.

The orchestration runs on the event loop; each blocking IMAP call is a dip into a thread
via :func:`asyncio.to_thread`, and only plain data crosses that line — the ORM rows stay
on the loop.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.imap.backend import IdentityLost, MailBackend, MailboxUnhealthy, StoreResult
from mailmind.imap.sync import flags_hash
from mailmind.suggest import staleness

#: Flag changes can be conditional because STORE takes UNCHANGEDSINCE.  MOVE cannot,
#: so it asks for the narrowest window available and says that is what it got.
PRECONDITION = {
    m.Operation.add_flag: m.Precondition.conditional,
    m.Operation.remove_flag: m.Precondition.conditional,
    m.Operation.delete: m.Precondition.conditional,
    m.Operation.move: m.Precondition.best_effort,
    # DELETE has no UNCHANGEDSINCE either.  The folder is looked at immediately before it
    # goes, which is a narrower window and not a promise, and is reported as what it is.
    m.Operation.discard_container: m.Precondition.best_effort,
}


class NotApplicable(Exception):
    pass


async def apply_bundle(
    scope: TenantScope,
    bundle: m.Bundle,
    backend: MailBackend,
    *,
    trash_container: m.Container | None = None,
) -> list[m.ApplyAttempt]:
    if bundle.status is not m.BundleStatus.accepted:
        raise NotApplicable(f"a {bundle.status.value} bundle is not applied")

    account = await scope.get(m.Account, bundle.account_id)
    if account.health is not m.AccountHealth.ok:
        raise NotApplicable(
            f"account {account.name} is {account.health.value}; "
            "suggestions are not applied against an account that is not healthy"
        )

    ordered = _in_order(bundle)
    if ordered:
        # Only once there is something to put in it.  A bundle whose every item died
        # between acceptance and here should leave no trace, and a folder nothing was
        # ever moved into is a trace.
        await _ensure_target_exists(scope, bundle, backend)

    attempts = []
    for suggestion in ordered:
        attempts.append(
            await _apply_one(
                scope, bundle, suggestion, backend, trash_container=trash_container
            )
        )

    if not attempts:
        # Every item died between being accepted and getting here.  Nothing was done to
        # the mailbox, so the bundle does not get to say it was: `applied == len(attempts)`
        # holds trivially of zero attempts, and the queue then showed a change that never
        # happened.  The bundle stays accepted with its items marked, which is the truth.
        raise NotApplicable(
            "no item of this bundle is still accepted; every one of them went stale "
            "before it could be applied, and nothing was done to the mailbox"
        )

    applied = sum(1 for a in attempts if a.outcome is m.ApplyOutcome.applied)
    bundle.status = (
        m.BundleStatus.applied if applied == len(attempts) else m.BundleStatus.partially_applied
    )
    await scope.audit(
        "bundle_applied",
        actor_kind="service",
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"applied": applied, "attempted": len(attempts)},
    )
    return attempts


def _in_order(bundle: m.Bundle) -> list[m.Suggestion]:
    """The accepted items, in the order they have to happen.

    For everything but a discard the order is immaterial and this is the list as it
    stands.  Discarding is different, because a bundle may hold a parent as long as it
    also holds every child, and what a server does when asked to remove a folder that
    still has folders under it is not settled — RFC 3501 lets it refuse, and lets it drop
    the name and leave the children orphaned under it.  Both happen; Dovecot does the
    second.

    Deepest first sidesteps the question.  By the time the parent's turn comes, the
    children that made it a parent are gone, and the two kinds of server behave the same.
    """
    accepted = [s for s in bundle.suggestions if s.status is m.SuggestionStatus.accepted]
    if bundle.operation not in m.CONTAINER_OPERATIONS:
        return accepted

    def depth(suggestion: m.Suggestion) -> tuple[int, str]:
        container = suggestion.source_container
        delimiter = container.delimiter
        levels = container.name.count(delimiter) if delimiter else 0
        return (-levels, container.name)

    return sorted(accepted, key=depth)


async def _ensure_target_exists(
    scope: TenantScope, bundle: m.Bundle, backend: MailBackend
) -> None:
    """Make the folder the bundle is about to move mail into, if it is not there yet.

    This is the moment the person's acceptance turns into a folder.  Nothing before it
    asked the server for anything: the target has been a row saying "proposed" since the
    bundle was made, precisely so that what the reviewer read and what happens here are
    the same folder.

    A refusal stops the whole bundle before a single message moves.  There is nowhere to
    put them, and a half-applied bundle whose other half had nowhere to go is worse than
    one that did nothing and said so.
    """
    target = bundle.target_container
    if target is None or target.exists_on_server:
        return

    try:
        info = await asyncio.to_thread(backend.create_container, target.name)
    except MailboxUnhealthy as exc:
        raise NotApplicable(
            f"the folder {target.name} could not be created: {exc}. Nothing was done to "
            "the mailbox, and this bundle can be accepted again once that is sorted out."
        ) from exc

    # What the server made, not what was asked for: a server may normalise the name or
    # impose its own separator, and the row should describe the folder that exists.
    target.name = info.name
    target.delimiter = info.delimiter
    target.special_use = info.special_use
    target.selectable = info.selectable
    target.exists_on_server = True
    target.discarded_at = None
    await scope.audit(
        "container_created",
        actor_kind="service",
        subject_kind="container",
        subject_id=target.id,
        payload={"name": target.name, "bundle_id": bundle.id},
    )


async def _apply_one(
    scope: TenantScope,
    bundle: m.Bundle,
    suggestion: m.Suggestion,
    backend: MailBackend,
    *,
    trash_container: m.Container | None,
) -> m.ApplyAttempt:
    precondition = PRECONDITION[bundle.operation]

    # The second check.  A person has already said yes, which is exactly why this one
    # matters more than the first.
    verdict = await staleness.check(scope, suggestion)
    if not verdict.fresh:
        suggestion.status = m.SuggestionStatus.stale
        suggestion.stale_detail = verdict.detail
        return await _record(
            scope,
            suggestion,
            precondition,
            precondition,
            m.ApplyOutcome.refused_stale,
            verdict.detail,
        )

    source = await scope.get(m.Container, suggestion.source_container_id)

    if suggestion.message_id is not None and suggestion.premise_modseq is None:
        # Without CONDSTORE the premise is only a flag fingerprint, and the cache it was
        # checked against may be older than the mailbox.  Looking at the server directly
        # narrows the window; it does not close it, which is why the guarantee reported
        # below is still best effort.
        observed = await asyncio.to_thread(
            backend.fetch_envelopes, source.name, [suggestion.premise_uid]
        )
        if not observed:
            suggestion.status = m.SuggestionStatus.stale
            suggestion.stale_detail = f"the message has left {source.name}"
            return await _record(
                scope,
                suggestion,
                precondition,
                m.Precondition.best_effort,
                m.ApplyOutcome.refused_stale,
                suggestion.stale_detail,
            )
        if flags_hash(observed[0].flags) != suggestion.premise_flags_hash:
            suggestion.status = m.SuggestionStatus.stale
            suggestion.stale_detail = (
                f"the message's flags changed on the server (now: "
                f"{' '.join(sorted(observed[0].flags)) or 'none'})"
            )
            return await _record(
                scope,
                suggestion,
                precondition,
                m.Precondition.best_effort,
                m.ApplyOutcome.refused_stale,
                suggestion.stale_detail,
            )

    # Plain values only from here down: `_perform` runs in a thread, and ORM rows do not.
    target = bundle.target_container
    try:
        result = await asyncio.to_thread(
            _perform,
            backend,
            bundle.operation,
            source_name=source.name,
            source_exists_on_server=source.exists_on_server,
            source_delimiter=source.delimiter,
            premise_uid=suggestion.premise_uid,
            premise_modseq=suggestion.premise_modseq,
            flag=bundle.flag,
            target_name=target.name if target is not None else None,
            trash_name=trash_container.name if trash_container is not None else None,
        )
    except MailboxUnhealthy as exc:
        suggestion.status = m.SuggestionStatus.failed
        return await _record(
            scope, suggestion, precondition, precondition, m.ApplyOutcome.failed, str(exc)
        )

    guarantee = m.Precondition(result.guarantee)
    if not result.changed:
        # Refused because the message moved under us between the check and the command.
        suggestion.status = m.SuggestionStatus.stale
        suggestion.stale_detail = result.detail or "the server declined; the message changed"
        return await _record(
            scope,
            suggestion,
            precondition,
            guarantee,
            m.ApplyOutcome.refused_stale,
            result.detail,
        )

    suggestion.status = m.SuggestionStatus.applied
    await _update_cache(scope, bundle, suggestion, result.resulting_uid)
    return await _record(
        scope,
        suggestion,
        precondition,
        guarantee,
        m.ApplyOutcome.applied,
        result.detail,
        resulting_uid=result.resulting_uid,
    )


def _perform(
    backend: MailBackend,
    operation: m.Operation,
    *,
    source_name: str,
    source_exists_on_server: bool,
    source_delimiter: str | None,
    premise_uid: int | None,
    premise_modseq: int | None,
    flag: str | None,
    target_name: str | None,
    trash_name: str | None,
) -> StoreResult:
    """One operation against the server, as a single dip.

    Deliberately sync, and called through :func:`asyncio.to_thread`: everything here is
    backend calls and plain data, so the whole exchange — for a discard, the
    select-then-list-then-delete sequence — happens off the loop in one go.
    """
    if operation in (m.Operation.add_flag, m.Operation.remove_flag):
        return backend.store_flags(
            source_name,
            premise_uid,
            (flag,),
            add=operation is m.Operation.add_flag,
            unchanged_since=premise_modseq,
        )
    if operation is m.Operation.move:
        return backend.move(
            source_name,
            premise_uid,
            target_name,
            expected_modseq=premise_modseq,
        )
    if operation is m.Operation.discard_container:
        return _discard(
            backend,
            source_name,
            exists_on_server=source_exists_on_server,
            delimiter=source_delimiter,
        )
    if operation is m.Operation.delete:
        # Deleting is moving to Trash, and nothing more: no \Deleted, no expunge.  01
        # says mail has no undo, and this is the one place that could prove it.
        if trash_name is None:
            raise MailboxUnhealthy("no Trash container is known for this account")
        moved = backend.move(
            source_name,
            premise_uid,
            trash_name,
            expected_modseq=premise_modseq,
        )
        return moved
    raise NotApplicable(f"unsupported operation {operation}")


def _discard(
    backend: MailBackend,
    name: str,
    *,
    exists_on_server: bool,
    delimiter: str | None,
) -> StoreResult:
    """Remove a folder, having just made sure there is nothing in it.

    This is the second check for a discard, and it goes to the server.  The first one
    asked the cache, which can be older than the mailbox — and the gap between them is
    exactly where a forgotten filter drops mail into a folder that looked abandoned.
    """
    if not exists_on_server:
        # Proposed and never made: the bundle that would have created it was never
        # accepted, or this is the same bundle undoing its own idea.  Nothing to ask the
        # server for, and the folder is gone in the only sense it ever existed.
        return StoreResult(True, "best_effort", "the folder had never been made")

    try:
        selected = backend.select(name, readonly=True)
    except IdentityLost:
        return StoreResult(False, "best_effort", f"{name} is already gone from the server")

    if selected.message_count:
        return StoreResult(
            False,
            "best_effort",
            f"{name} now holds {selected.message_count} messages, so it is no "
            "longer a folder whose removal cannot lose mail",
        )

    # Asked again per folder rather than once per bundle, because the deletions in this
    # bundle are themselves changing the answer as they land.
    children = sorted(
        info.name
        for info in backend.list_containers()
        if delimiter and info.name.startswith(name + delimiter)
    )
    if children:
        return StoreResult(
            False,
            "best_effort",
            f"{name} still has folders under it: {', '.join(children)}",
        )

    backend.delete_container(name)
    return StoreResult(True, "best_effort")


async def _update_cache(scope: TenantScope, bundle, suggestion, resulting_uid) -> None:  # noqa: ANN001
    """Reflect what we just did, so the next staleness check does not see our own change."""
    if bundle.operation is m.Operation.discard_container:
        container = await scope.get(m.Container, suggestion.source_container_id)
        # Marked, not deleted: this suggestion still points at the row, and so may older
        # placements.  A reviewer reading what happened deserves a folder with a date on
        # it rather than a dangling id.
        container.discarded_at = dt.datetime.now(dt.UTC)
        container.exists_on_server = False
        container.message_count = 0
        return

    placement = await scope.scalar(
        sa.select(m.Placement).where(
            m.Placement.container_id == suggestion.source_container_id,
            m.Placement.container_generation == suggestion.premise_container_generation,
            m.Placement.uid == suggestion.premise_uid,
        )
    )
    if placement is None:
        return
    if bundle.operation in (m.Operation.move, m.Operation.delete):
        placement.gone_at = dt.datetime.now(dt.UTC)
    elif bundle.operation is m.Operation.add_flag:
        placement.flags = " ".join(sorted(set(placement.flags.split()) | {bundle.flag}))
    elif bundle.operation is m.Operation.remove_flag:
        placement.flags = " ".join(sorted(set(placement.flags.split()) - {bundle.flag}))


async def _record(
    scope: TenantScope,
    suggestion: m.Suggestion,
    precondition: m.Precondition,
    guarantee: m.Precondition,
    outcome: m.ApplyOutcome,
    detail: str | None,
    *,
    resulting_uid: int | None = None,
) -> m.ApplyAttempt:
    attempt = m.ApplyAttempt(
        suggestion_id=suggestion.id,
        precondition=precondition,
        guarantee_obtained=guarantee,
        outcome=outcome,
        server_response=detail,
        resulting_uid=resulting_uid,
    )
    scope.add(attempt)
    await scope.audit(
        "apply_attempted",
        actor_kind="service",
        subject_kind="suggestion",
        subject_id=suggestion.id,
        payload={
            "outcome": outcome.value,
            "precondition": precondition.value,
            "guarantee_obtained": guarantee.value,
            "detail": detail,
        },
    )
    return attempt
