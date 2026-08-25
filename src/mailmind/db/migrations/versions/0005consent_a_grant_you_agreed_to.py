"""a grant you agreed to

Until now a grant was come by on the command line: `mailmindctl grant`, printed once, and
then somebody's problem to carry to a client by hand.  That step is where it broke — a
config naming an environment variable nothing set, substituted to the empty string, which
arrives as a bearer token that is not one and reads as "cannot connect".

MCP clients already know how to ask.  These three tables are what lets them: a client that
registered itself, one trip from `/authorize` through a consent page to a redeemable code,
and the credentials that come out of it.

The grant is unchanged and remains the whole of what an agent may do.  What is new is that
a person agrees to one on a page instead of copying a token, and that the credential the
agent holds is no longer the same object as the decision that was made — tokens rotate,
consent does not, which is what keeps the agents page listing one row per decision rather
than one per hour.

`grant.client_id` is the only column added to anything that existed.  It is null for every
grant minted on the command line, which is right: those are nobody's client in particular.

Revision ID: 0005consent
Revises: 0004phantom
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005consent"
down_revision: str | Sequence[str] | None = "0004phantom"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_client",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("client_name", sa.String(length=128), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("grant_types", sa.JSON(), nullable=False),
        sa.Column("response_types", sa.JSON(), nullable=False),
        sa.Column("scope", sa.String(length=256), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id"),
    )
    with op.batch_alter_table("oauth_client", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_client_tenant_id"), ["tenant_id"], unique=False
        )

    op.create_table(
        "oauth_authorization",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=512), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=512), nullable=True),
        sa.Column("code_challenge", sa.String(length=256), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_id", sa.Integer(), nullable=True),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["grant.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
        sa.UniqueConstraint("request_id"),
    )
    with op.batch_alter_table("oauth_authorization", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_authorization_client_id"), ["client_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_authorization_tenant_id"), ["tenant_id"], unique=False
        )

    op.create_table(
        "oauth_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("access", "refresh", name="oauth_token_kind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["grant.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("oauth_token", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_token_grant_id"), ["grant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_oauth_token_tenant_id"), ["tenant_id"], unique=False
        )

    # Plain, nullable, no constraint: SQLite takes a column added this way in one
    # statement, and has no ALTER that could add a foreign key anyway.  See 0002.
    op.add_column("grant", sa.Column("client_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("grant", schema=None) as batch_op:
        batch_op.drop_column("client_id")
    op.drop_table("oauth_token")
    op.drop_table("oauth_authorization")
    op.drop_table("oauth_client")
