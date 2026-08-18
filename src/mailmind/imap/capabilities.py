"""Declared, then probed.

04: each account carries a written-down list of what it can do, and the service checks
that list against reality rather than discovering capabilities at runtime and hoping.  A
capability that turns out to be missing is a loud failure, not a quiet downgrade.

The declaration decides what the service attempts.  The probe's job is only to prove the
declaration is still true.  Keeping those separate is what makes a capability-gated code
path trustworthy — if the probe drove behaviour, a server that quietly dropped CONDSTORE
would turn conditional applies into best-effort ones without anybody noticing.
"""

from __future__ import annotations

import datetime as dt

import attrs
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.scope import TenantScope
from mailmind.imap.backend import MailBackend


@attrs.frozen
class CapabilityReport:
    account: str
    #: Declared and not offered.  The dangerous direction.
    missing: tuple[str, ...]
    #: Offered and not declared.  Means the declaration is stale, not that anything broke.
    undeclared: tuple[str, ...]

    @property
    def diverged(self) -> bool:
        return bool(self.missing or self.undeclared)


def probe_account(
    scope: TenantScope, account: m.Account, backend: MailBackend
) -> CapabilityReport:
    """Compare what the server offers against what the account declares.

    04 asks what to do when this fails at three in the morning: stop, or keep reading and
    refuse to write.  This iteration keeps reading — a mailbox that can still be browsed
    and assessed is worth more than one that goes dark, and nothing is applied against an
    account that is not ``ok`` anyway.
    """
    offered = {c.upper() for c in backend.capabilities()}
    now = dt.datetime.now(dt.UTC)

    declared_rows = scope.scalars(
        sa.select(m.AccountCapability).where(m.AccountCapability.account_id == account.id)
    ).all()
    declared = {row.name for row in declared_rows if row.declared}

    for row in declared_rows:
        row.probed_present = row.name in offered
        row.probed_at = now

    for name in sorted(offered - {row.name for row in declared_rows}):
        scope.add(
            m.AccountCapability(
                account_id=account.id,
                name=name,
                declared=False,
                probed_present=True,
                probed_at=now,
            )
        )

    missing = tuple(sorted(declared - offered))
    undeclared = tuple(sorted(offered - declared))

    account.health_checked_at = now
    if missing:
        account.health = m.AccountHealth.read_only
        account.health_detail = (
            f"declared capabilities not offered by the server: {', '.join(missing)}"
        )
    else:
        account.health = m.AccountHealth.ok
        account.health_detail = None

    report = CapabilityReport(account=account.name, missing=missing, undeclared=undeclared)
    scope.audit(
        "capability_probe",
        actor_kind="service",
        subject_kind="account",
        subject_id=account.id,
        payload={
            "missing": list(missing),
            "undeclared": list(undeclared),
            "health": account.health.value,
        },
    )
    return report


def declares(scope: TenantScope, account_id: int, capability: str) -> bool:
    """What the service is allowed to attempt.  The declaration, never the probe."""
    return bool(
        scope.scalar(
            sa.select(m.AccountCapability.id).where(
                m.AccountCapability.account_id == account_id,
                m.AccountCapability.name == capability,
                m.AccountCapability.declared.is_(True),
            )
        )
    )
