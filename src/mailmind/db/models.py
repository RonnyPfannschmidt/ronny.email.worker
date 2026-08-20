"""The mailmind schema.

Every table carries ``tenant_id``.  Nothing else in this module enforces tenancy —
that is :mod:`mailmind.db.scope`, which applies the predicate to every ORM SELECT.
The rule the schema does hold up is that a tenant column is never nullable, so a row
whose tenant is unknown cannot be written at all.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(sa.types.TypeDecorator):
    """A datetime that is timezone-aware on both sides of the database.

    SQLite has no datetime type and no offset: an aware value handed to it is written
    without its offset and read back naive, so a column declared ``timezone=True`` is
    still a lie by the time anything compares it to ``now()``.  That comparison is not
    hypothetical — it is how a grant's expiry is checked on every request, and a naive
    value there raises ``TypeError`` rather than expiring anything.

    So: normalise to UTC going in, reattach UTC coming out.  A naive value is read as UTC
    rather than refused, because a ``Date`` header carrying the ``-0000`` that RFC 5322
    defines as "zone unknown" parses to a naive datetime, and that is ordinary mail.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: dt.datetime | None, dialect: object
    ) -> dt.datetime | None:
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: dt.datetime | None, dialect: object
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


def _enum(py_enum: type[enum.Enum], name: str) -> sa.Enum:
    """Store enums as their string values with a CHECK constraint, not as ints."""
    return sa.Enum(
        py_enum,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: sa.JSON,
        list[str]: sa.JSON,
        dt.datetime: UTCDateTime(),
    }


class TenantScoped:
    """Mixin marking a table as belonging to exactly one tenant.

    :mod:`mailmind.db.scope` filters on precisely the classes carrying this mixin, so
    inheriting from it is what opts a table into isolation.  A new table that forgets
    the mixin is visible across tenants, which is why ``test_scope`` asserts that every
    mapped class except :class:`Tenant` has it.
    """

    @sa.orm.declared_attr
    def tenant_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(sa.ForeignKey("tenant.id"), nullable=False, index=True)


# --------------------------------------------------------------------------- identity


class Tenant(Base):
    """A person, and everything of theirs.  Tenant zero is created by the bootstrap."""

    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class ProducerKind(enum.Enum):
    agent = "agent"
    service = "service"
    person = "person"


class Producer(Base, TenantScoped):
    """Whoever a suggestion or an assessment came from.

    The external opencode instance is one row.  The person reviewing is another, because
    a decision needs an actor too.  The service's own suggestion-finding will be a third
    when it exists.
    """

    __tablename__ = "producer"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ProducerKind] = mapped_column(_enum(ProducerKind, "producer_kind"))
    name: Mapped[str] = mapped_column(sa.String(128))
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    #: Which account this person is currently working in.  The local login is a key
    #: rather than an identity, so the reviewer is still implicit and the account is the
    #: thing they choose instead; on a deployment the same column is per authenticated
    #: person.  It says nothing about an agent, whose accounts come from its grant and are
    #: not chosen at all.
    #:
    #: ``SET NULL`` rather than a cascade: removing an account should drop a preference,
    #: never a producer, because the producer is what "who accepted this" points at.
    current_account_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("account.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (sa.UniqueConstraint("tenant_id", "name"),)


class Capability(enum.Enum):
    """What a grant may do.

    There is deliberately no ``apply``.  An agent cannot apply because applying is not a
    value this enum can hold, not because a check rejects it.
    """

    observe = "observe"
    suggest = "suggest"
    assess = "assess"


class Grant(Base, TenantScoped):
    """What a producer connected over MCP may see and say.

    The view is given, not chosen: a request carries a bearer token, the token resolves
    to a grant, and the grant supplies the tenant and the accounts.  Nothing a caller
    says can widen it.
    """

    __tablename__ = "grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    producer_id: Mapped[int] = mapped_column(sa.ForeignKey("producer.id"))
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    capabilities: Mapped[list[str]] = mapped_column(default=list)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    producer: Mapped[Producer] = relationship()
    accounts: Mapped[list[GrantAccount]] = relationship(
        back_populates="grant", cascade="all, delete-orphan"
    )

    def allows(self, capability: Capability) -> bool:
        return capability.value in self.capabilities


class GrantAccount(Base, TenantScoped):
    """Which accounts a grant covers.  No rows means no mail, not all mail."""

    __tablename__ = "grant_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    grant_id: Mapped[int] = mapped_column(sa.ForeignKey("grant.id"))
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("account.id"))

    grant: Mapped[Grant] = relationship(back_populates="accounts")

    __table_args__ = (sa.UniqueConstraint("grant_id", "account_id"),)


# --------------------------------------------------------------------- mailbox access


class BackendKind(enum.Enum):
    imap = "imap"


class AccountHealth(enum.Enum):
    """Suggestions are not applied against an account that is not ``ok``."""

    unknown = "unknown"
    ok = "ok"
    read_only = "read_only"
    down = "down"


class Account(Base, TenantScoped):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(128))
    backend: Mapped[BackendKind] = mapped_column(
        _enum(BackendKind, "backend_kind"), default=BackendKind.imap
    )
    host: Mapped[str] = mapped_column(sa.String(255))
    port: Mapped[int] = mapped_column(default=993)
    use_ssl: Mapped[bool] = mapped_column(default=True)
    username: Mapped[str] = mapped_column(sa.String(255))
    #: Where the password is found, never the password: an ``env://``, ``file://`` or
    #: ``secret-storage://`` URL.
    password_url: Mapped[str] = mapped_column(sa.String(255))
    health: Mapped[AccountHealth] = mapped_column(
        _enum(AccountHealth, "account_health"), default=AccountHealth.unknown
    )
    health_detail: Mapped[str | None] = mapped_column(sa.Text, default=None)
    health_checked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    #: When false, bodies are fetched for display and never written to the cache.
    cache_bodies: Mapped[bool] = mapped_column(default=True)

    capabilities: Mapped[list[AccountCapability]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    containers: Mapped[list[Container]] = relationship(back_populates="account")

    __table_args__ = (sa.UniqueConstraint("tenant_id", "name"),)


class AccountCapability(Base, TenantScoped):
    """Declared, then probed.

    ``declared`` comes from configuration and decides what the service will attempt.
    ``probed_present`` is what the server actually announced.  A divergence either way is
    a loud failure: a capability that turns out to be missing must not become a quiet
    downgrade, and one that appears unannounced means the declaration is stale.
    """

    __tablename__ = "account_capability"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("account.id"))
    name: Mapped[str] = mapped_column(sa.String(32))
    declared: Mapped[bool] = mapped_column(default=True)
    probed_present: Mapped[bool | None] = mapped_column(default=None)
    probed_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    account: Mapped[Account] = relationship(back_populates="capabilities")

    __table_args__ = (sa.UniqueConstraint("account_id", "name"),)


class Container(Base, TenantScoped):
    """A folder, or later a label.  Both are containers a message sits in."""

    __tablename__ = "container"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("account.id"))
    name: Mapped[str] = mapped_column(sa.String(512))
    delimiter: Mapped[str | None] = mapped_column(sa.String(4), default=None)
    special_use: Mapped[str | None] = mapped_column(sa.String(32), default=None)
    selectable: Mapped[bool] = mapped_column(default=True)

    uidvalidity: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    uidnext: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    highestmodseq: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    #: Bumped whenever UIDVALIDITY changes.  Everything remembered about the container
    #: under an older generation is suspect, and suggestions resting on it are dead.
    generation: Mapped[int] = mapped_column(default=1)

    message_count: Mapped[int] = mapped_column(default=0)
    last_full_sync_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    last_incremental_sync_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    account: Mapped[Account] = relationship(back_populates="containers")

    __table_args__ = (sa.UniqueConstraint("account_id", "name"),)


# ----------------------------------------------------------------------------- cache


class ParseStatus(enum.Enum):
    ok = "ok"
    partial = "partial"
    unparseable = "unparseable"


class Message(Base, TenantScoped):
    """A mail item as far as we can tell, cached from the server.

    Identity is the ``Message-ID`` header where there is one and ``content_key`` where
    there is not — a hash over what the envelope does provide.  Neither is trustworthy;
    two messages may share a header and one may have none.  ``content_key`` is therefore
    only unique per account, and matching across containers is a convenience, not a fact.
    """

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("account.id"))

    message_id_header: Mapped[str | None] = mapped_column(sa.String(512), default=None)
    content_key: Mapped[str] = mapped_column(sa.String(64))

    subject: Mapped[str | None] = mapped_column(sa.Text, default=None)
    date_header: Mapped[dt.datetime | None] = mapped_column(default=None)
    from_address: Mapped[str | None] = mapped_column(sa.String(320), default=None)
    from_display: Mapped[str | None] = mapped_column(sa.Text, default=None)
    size_bytes: Mapped[int | None] = mapped_column(default=None)
    has_attachments: Mapped[bool] = mapped_column(default=False)
    list_id: Mapped[str | None] = mapped_column(sa.String(255), default=None, index=True)
    has_list_unsubscribe: Mapped[bool] = mapped_column(default=False)
    in_reply_to: Mapped[str | None] = mapped_column(sa.String(512), default=None)
    preview: Mapped[str | None] = mapped_column(sa.Text, default=None)
    parse_status: Mapped[ParseStatus] = mapped_column(
        _enum(ParseStatus, "parse_status"), default=ParseStatus.ok
    )
    cached_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    placements: Mapped[list[Placement]] = relationship(back_populates="message")
    addresses: Mapped[list[MessageAddress]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.UniqueConstraint("account_id", "content_key"),
        sa.Index("ix_message_from", "tenant_id", "account_id", "from_address"),
    )


class Placement(Base, TenantScoped):
    """Where a message currently sits, and what the server said about it there.

    This is the row a premise is taken from and the row staleness is checked against.
    IMAP writes one live placement per message per container; the table needs no change
    when a backend offers multi-membership.
    """

    __tablename__ = "placement"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(sa.ForeignKey("message.id"))
    container_id: Mapped[int] = mapped_column(sa.ForeignKey("container.id"))

    uid: Mapped[int] = mapped_column(sa.BigInteger)
    #: The generation the UID was observed under.  A UID means nothing without it.
    container_generation: Mapped[int] = mapped_column()
    modseq: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    flags: Mapped[str] = mapped_column(sa.Text, default="")
    internaldate: Mapped[dt.datetime | None] = mapped_column(default=None)

    seen_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    #: Set when the message is no longer in this container.  Rows are not deleted,
    #: because a suggestion may still point at one and the reviewer deserves to be told
    #: what happened rather than shown a dangling reference.
    gone_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    message: Mapped[Message] = relationship(back_populates="placements")
    container: Mapped[Container] = relationship()

    __table_args__ = (
        sa.UniqueConstraint("container_id", "container_generation", "uid"),
        sa.Index("ix_placement_live", "tenant_id", "container_id", "gone_at"),
    )


class AddressRole(enum.Enum):
    from_ = "from"
    to = "to"
    cc = "cc"
    bcc = "bcc"
    reply_to = "reply_to"


class MessageAddress(Base, TenantScoped):
    """A parsed address off a message.

    Identity is the address; the display name is decoration and identifies nobody.  Both
    are kept because the disagreement between them is a mechanical finding.
    """

    __tablename__ = "message_address"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(sa.ForeignKey("message.id"))
    role: Mapped[AddressRole] = mapped_column(_enum(AddressRole, "address_role"))
    address: Mapped[str] = mapped_column(sa.String(320))
    display_name: Mapped[str | None] = mapped_column(sa.Text, default=None)

    message: Mapped[Message] = relationship(back_populates="addresses")

    __table_args__ = (sa.Index("ix_message_address", "tenant_id", "address"),)


class MessageBody(Base, TenantScoped):
    """Text parts, fetched on demand.

    Separate from :class:`Message` so the hot path stays small, eviction is a delete, and
    an account configured not to cache bodies simply has no rows here.
    """

    __tablename__ = "message_body"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(sa.ForeignKey("message.id"), unique=True)
    text_plain: Mapped[str | None] = mapped_column(sa.Text, default=None)
    #: Extracted from text/html, never rendered as HTML from here.
    text_from_html: Mapped[str | None] = mapped_column(sa.Text, default=None)
    links: Mapped[dict[str, Any]] = mapped_column(default=dict)
    attachments: Mapped[dict[str, Any]] = mapped_column(default=dict)
    bytes_stored: Mapped[int] = mapped_column(default=0)
    cached_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_read_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    message: Mapped[Message] = relationship()


# ----------------------------------------------------------------------- suggestions


class ActionKind(enum.Enum):
    state = "state"
    draft = "draft"


class Operation(enum.Enum):
    move = "move"
    add_flag = "add_flag"
    remove_flag = "remove_flag"
    delete = "delete"


class BundleStatus(enum.Enum):
    proposed = "proposed"
    accepted = "accepted"
    applied = "applied"
    partially_applied = "partially_applied"
    rejected = "rejected"
    withdrawn = "withdrawn"
    superseded = "superseded"
    expired = "expired"


class Bundle(Base, TenantScoped):
    """The reviewed unit.

    A bundle is homogeneous — one operation, one target — so that showing its full effect
    is possible and accepting it is one deliberate act over an enumerated list rather than
    a bulk accept over things not looked at.  A lone suggestion is a bundle of one; there
    is no un-bundled path.
    """

    __tablename__ = "bundle"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("account.id"))
    producer_id: Mapped[int] = mapped_column(sa.ForeignKey("producer.id"))

    action_kind: Mapped[ActionKind] = mapped_column(_enum(ActionKind, "action_kind"))
    operation: Mapped[Operation] = mapped_column(_enum(Operation, "operation"))
    target_container_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("container.id"), default=None
    )
    flag: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)

    #: What the mail is about, as the producer read it.  Sits alongside the effect in
    #: review, never instead of it.
    summary: Mapped[str] = mapped_column(sa.Text)
    #: Why this is proposed.
    reason: Mapped[str] = mapped_column(sa.Text)

    status: Mapped[BundleStatus] = mapped_column(
        _enum(BundleStatus, "bundle_status"), default=BundleStatus.proposed, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column()
    decided_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    decided_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("producer.id"), default=None
    )
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, default=None)

    account: Mapped[Account] = relationship()
    producer: Mapped[Producer] = relationship(foreign_keys=[producer_id])
    decided_by: Mapped[Producer | None] = relationship(foreign_keys=[decided_by_id])
    target_container: Mapped[Container | None] = relationship()
    suggestions: Mapped[list[Suggestion]] = relationship(
        back_populates="bundle", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.CheckConstraint(
            "(operation != 'move') OR (target_container_id IS NOT NULL)",
            name="ck_move_needs_target",
        ),
        sa.CheckConstraint(
            "(operation NOT IN ('add_flag', 'remove_flag')) OR (flag IS NOT NULL)",
            name="ck_flag_op_needs_flag",
        ),
    )


class SuggestionStatus(enum.Enum):
    proposed = "proposed"
    #: Dropped by the reviewer before accepting the rest.
    excluded = "excluded"
    accepted = "accepted"
    applied = "applied"
    rejected = "rejected"
    withdrawn = "withdrawn"
    superseded = "superseded"
    #: The premise moved on.  Never applied to whatever happens to be there now.
    stale = "stale"
    expired = "expired"
    failed = "failed"


class Suggestion(Base, TenantScoped):
    """One operation over one message, and what it was computed against.

    The premise columns are the whole point of the row.  They are checked twice — before
    the bundle is shown and again immediately before this item is applied — because both
    gaps are real, and the second is the dangerous one, a person having already said yes.
    """

    __tablename__ = "suggestion"

    id: Mapped[int] = mapped_column(primary_key=True)
    bundle_id: Mapped[int] = mapped_column(sa.ForeignKey("bundle.id"))
    message_id: Mapped[int] = mapped_column(sa.ForeignKey("message.id"))
    source_container_id: Mapped[int] = mapped_column(sa.ForeignKey("container.id"))

    premise_container_generation: Mapped[int] = mapped_column()
    premise_uid: Mapped[int] = mapped_column(sa.BigInteger)
    premise_modseq: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    premise_flags_hash: Mapped[str] = mapped_column(sa.String(64), default="")

    status: Mapped[SuggestionStatus] = mapped_column(
        _enum(SuggestionStatus, "suggestion_status"),
        default=SuggestionStatus.proposed,
        index=True,
    )
    #: What changed, when the premise stopped holding.  Shown to the reviewer instead of
    #: the item silently disappearing.
    stale_detail: Mapped[str | None] = mapped_column(sa.Text, default=None)

    bundle: Mapped[Bundle] = relationship(back_populates="suggestions")
    message: Mapped[Message] = relationship()
    source_container: Mapped[Container] = relationship()

    __table_args__ = (sa.UniqueConstraint("bundle_id", "message_id"),)


# ----------------------------------------------------------------------- assessments


class SubjectKind(enum.Enum):
    message = "message"
    bundle = "bundle"


class AssessmentOrigin(enum.Enum):
    #: Computed by the service from the mail itself.  No model involved, so it cannot be
    #: talked into a wrong reading.
    mechanical = "mechanical"
    #: Supplied over MCP by a producer.
    producer = "producer"


class Assessment(Base, TenantScoped):
    """How trustworthy the mail looks.

    02 says this must not come from the producer of the suggestion.  With a single agent
    configured that cannot yet be enforced, so both ids are recorded and the review UI
    says plainly when they are the same.  Making it a constraint is what a second producer
    identity buys.
    """

    __tablename__ = "assessment"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_kind: Mapped[SubjectKind] = mapped_column(_enum(SubjectKind, "subject_kind"))
    subject_id: Mapped[int] = mapped_column()
    origin: Mapped[AssessmentOrigin] = mapped_column(
        _enum(AssessmentOrigin, "assessment_origin")
    )
    producer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("producer.id"), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    producer: Mapped[Producer | None] = relationship()
    findings: Mapped[list[Finding]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.Index("ix_assessment_subject", "tenant_id", "subject_kind", "subject_id"),
        sa.CheckConstraint(
            "(origin != 'producer') OR (producer_id IS NOT NULL)",
            name="ck_producer_assessment_has_producer",
        ),
    )


class FindingClass(enum.Enum):
    #: Decidable without a model, and therefore not something an agent can talk its way
    #: around.  Only the service writes these.
    mechanical = "mechanical"
    #: What a model made of it.  Useful and not decidable; marked as such.
    interpretation = "interpretation"


class Finding(Base, TenantScoped):
    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(sa.ForeignKey("assessment.id"))
    finding_class: Mapped[FindingClass] = mapped_column(_enum(FindingClass, "finding_class"))
    code: Mapped[str] = mapped_column(sa.String(64))
    detail: Mapped[str] = mapped_column(sa.Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)

    assessment: Mapped[Assessment] = relationship(back_populates="findings")


# --------------------------------------------------------------------------- record


class Precondition(enum.Enum):
    #: Must fail rather than act if the message changed underneath.
    conditional = "conditional"
    #: May act on a weaker promise, but has to say that is what it got.
    best_effort = "best_effort"


class ApplyOutcome(enum.Enum):
    applied = "applied"
    refused_stale = "refused_stale"
    failed = "failed"


class ApplyAttempt(Base, TenantScoped):
    """What was actually done to the mailbox, and what promise was actually obtained.

    ``precondition`` is what the operation asked for and ``guarantee_obtained`` is what
    the server could give.  They differ for MOVE, which has no UNCHANGEDSINCE, and the
    difference is reported rather than glossed.
    """

    __tablename__ = "apply_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(sa.ForeignKey("suggestion.id"))
    attempted_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    precondition: Mapped[Precondition] = mapped_column(_enum(Precondition, "precondition"))
    guarantee_obtained: Mapped[Precondition] = mapped_column(
        _enum(Precondition, "guarantee_obtained")
    )
    outcome: Mapped[ApplyOutcome] = mapped_column(_enum(ApplyOutcome, "apply_outcome"))
    server_response: Mapped[str | None] = mapped_column(sa.Text, default=None)
    #: From UIDPLUS COPYUID, where the server offers it.
    resulting_uid: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)

    suggestion: Mapped[Suggestion] = relationship()


class AuditEvent(Base, TenantScoped):
    """The record: what happened, in order, kept rather than overwritten.

    Insert-only.  The state tables stay authoritative — this is not an event log that
    everything is folded from, it is the thing that can explain what happened to
    someone's mail and answer "who accepted this".  Whether that turns out to be enough
    is the open question this iteration exists to answer.
    """

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column()
    at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    actor_kind: Mapped[str] = mapped_column(sa.String(32))
    actor_id: Mapped[int | None] = mapped_column(default=None)
    subject_kind: Mapped[str] = mapped_column(sa.String(32))
    subject_id: Mapped[int | None] = mapped_column(default=None)
    verb: Mapped[str] = mapped_column(sa.String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "seq"),
        sa.Index("ix_audit_subject", "tenant_id", "subject_kind", "subject_id"),
    )
