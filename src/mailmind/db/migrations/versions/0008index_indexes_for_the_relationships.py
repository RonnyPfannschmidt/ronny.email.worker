"""indexes for the relationships

Following a message to its addresses or placements, a bundle to its items, or an
assessment to its findings had only the tenant index to go by.  With one tenant that is
the whole table, read once per message loaded.

Revision ID: 0008index
Revises: 0007task
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008index"
down_revision: str | Sequence[str] | None = "0007task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = (
    ("ix_placement_message", "placement", "message_id"),
    ("ix_message_address_message", "message_address", "message_id"),
    ("ix_suggestion_bundle", "suggestion", "bundle_id"),
    ("ix_suggestion_message", "suggestion", "message_id"),
    ("ix_finding_assessment", "finding", "assessment_id"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, ["tenant_id", column])


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
