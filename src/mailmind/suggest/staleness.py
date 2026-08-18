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


def refresh_bundle(scope: TenantScope, bundle: m.Bundle) -> dict[int, Freshness]:
    """Check every live item and mark the ones that have died.

    Called before the bundle is shown.  Items that have gone stale are marked, not
    hidden: 03 says a reviewer is told what changed.
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
    return verdicts
