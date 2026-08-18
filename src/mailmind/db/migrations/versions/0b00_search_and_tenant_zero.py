"""Message search, and tenant zero.

The search table is a standalone FTS5 index rather than an external-content one, so it can
carry tenant_id as a column.  That matters: this is the one place raw SQL reaches the
database, and it takes the tenant as a bound parameter instead of relying on the ORM
loader criteria that do not apply here.

Revision ID: 0b00search
Revises: 0a3afd7ba279
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0b00search"
down_revision = "0a3afd7ba279"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE message_fts USING fts5(
            subject,
            from_text,
            preview,
            message_id UNINDEXED,
            tenant_id UNINDEXED,
            account_id UNINDEXED,
            tokenize = "unicode61 remove_diacritics 2"
        )
        """
    )
    op.execute(
        sa.text(
            "INSERT INTO tenant (id, name, created_at) "
            "VALUES (0, 'tenant-zero', CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS message_fts")
    op.execute("DELETE FROM tenant WHERE id = 0")
