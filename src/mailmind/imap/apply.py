"""The only place anything is written to a mailbox.

Note who can reach it: this is called by the review flow, never from the MCP surface.
The applier takes resolved operations and does not read mail; the producer reads mail and
cannot reach here.  No row of 02's table has two of the three.

Each operation declares how strong a guarantee it needs, and records what it actually
got.  Those differ for MOVE, and the difference is reported rather than glossed over.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.imap.backend import MailBackend, MailboxUnhealthy
from mailmind.imap.sync import flags_hash
from mailmind.suggest import staleness

#: Flag changes can be conditional because STORE takes UNCHANGEDSINCE.  MOVE cannot,
#: so it asks for the narrowest window available and says that is what it got.
PRECONDITION = {
    m.Operation.add_flag: m.Precondition.conditional,
    m.Operation.remove_flag: m.Precondition.conditional,
    m.Operation.delete: m.Precondition.conditional,
    m.Operation.move: m.Precondition.best_effort,
}


class NotApplicable(Exception):
    pass


def apply_bundle(
    scope: TenantScope,
    bundle: m.Bundle,
    backend: MailBackend,
    *,
    trash_container: m.Container | None = None,
) -> list[m.ApplyAttempt]:
    if bundle.status is not m.BundleStatus.accepted:
        raise NotApplicable(f"a {bundle.status.value} bundle is not applied")

    account = scope.get(m.Account, bundle.account_id)
    if account.health is not m.AccountHealth.ok:
        raise NotApplicable(
            f"account {account.name} is {account.health.value}; "
            "suggestions are not applied against an account that is not healthy"
        )

    attempts = []
    for suggestion in bundle.suggestions:
        if suggestion.status is not m.SuggestionStatus.accepted:
            continue
        attempts.append(
            _apply_one(scope, bundle, suggestion, backend, trash_container=trash_container)
        )

    applied = sum(1 for a in attempts if a.outcome is m.ApplyOutcome.applied)
    bundle.status = (
        m.BundleStatus.applied if applied == len(attempts) else m.BundleStatus.partially_applied
    )
    scope.audit(
        "bundle_applied",
        actor_kind="service",
        subject_kind="bundle",
        subject_id=bundle.id,
        payload={"applied": applied, "attempted": len(attempts)},
    )
    return attempts


def _apply_one(
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
    verdict = staleness.check(scope, suggestion)
    if not verdict.fresh:
        suggestion.status = m.SuggestionStatus.stale
        suggestion.stale_detail = verdict.detail
        return _record(
            scope,
            suggestion,
            precondition,
            precondition,
            m.ApplyOutcome.refused_stale,
            verdict.detail,
        )

    source = scope.get(m.Container, suggestion.source_container_id)

    if suggestion.premise_modseq is None:
        # Without CONDSTORE the premise is only a flag fingerprint, and the cache it was
        # checked against may be older than the mailbox.  Looking at the server directly
        # narrows the window; it does not close it, which is why the guarantee reported
        # below is still best effort.
        observed = backend.fetch_envelopes(source.name, [suggestion.premise_uid])
        if not observed:
            suggestion.status = m.SuggestionStatus.stale
            suggestion.stale_detail = f"the message has left {source.name}"
            return _record(
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
            return _record(
                scope,
                suggestion,
                precondition,
                m.Precondition.best_effort,
                m.ApplyOutcome.refused_stale,
                suggestion.stale_detail,
            )

    try:
        result = _perform(bundle, suggestion, backend, source, trash_container)
    except MailboxUnhealthy as exc:
        suggestion.status = m.SuggestionStatus.failed
        return _record(
            scope, suggestion, precondition, precondition, m.ApplyOutcome.failed, str(exc)
        )

    guarantee = m.Precondition(result.guarantee)
    if not result.changed:
        # Refused because the message moved under us between the check and the command.
        suggestion.status = m.SuggestionStatus.stale
        suggestion.stale_detail = result.detail or "the server declined; the message changed"
        return _record(
            scope,
            suggestion,
            precondition,
            guarantee,
            m.ApplyOutcome.refused_stale,
            result.detail,
        )

    suggestion.status = m.SuggestionStatus.applied
    _update_cache(scope, bundle, suggestion, result.resulting_uid)
    return _record(
        scope,
        suggestion,
        precondition,
        guarantee,
        m.ApplyOutcome.applied,
        result.detail,
        resulting_uid=result.resulting_uid,
    )


def _perform(bundle, suggestion, backend, source, trash_container):  # noqa: ANN001
    if bundle.operation in (m.Operation.add_flag, m.Operation.remove_flag):
        return backend.store_flags(
            source.name,
            suggestion.premise_uid,
            (bundle.flag,),
            add=bundle.operation is m.Operation.add_flag,
            unchanged_since=suggestion.premise_modseq,
        )
    if bundle.operation is m.Operation.move:
        target = bundle.target_container
        return backend.move(
            source.name,
            suggestion.premise_uid,
            target.name,
            expected_modseq=suggestion.premise_modseq,
        )
    if bundle.operation is m.Operation.delete:
        # Deleting is moving to Trash and flagging.  Nothing here expunges: 01 says mail
        # has no undo, and this is the one place that could prove it.
        if trash_container is None:
            raise MailboxUnhealthy("no Trash container is known for this account")
        moved = backend.move(
            source.name,
            suggestion.premise_uid,
            trash_container.name,
            expected_modseq=suggestion.premise_modseq,
        )
        return moved
    raise NotApplicable(f"unsupported operation {bundle.operation}")


def _update_cache(scope: TenantScope, bundle, suggestion, resulting_uid) -> None:  # noqa: ANN001
    """Reflect what we just did, so the next staleness check does not see our own change."""
    placement = scope.scalar(
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


def _record(
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
    scope.audit(
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
