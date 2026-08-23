"""why a message parsed partially

``parse_status`` said a message was damaged and nothing said how.  On a real mailbox that
was 16,755 messages of 29,079, almost all of them ordinary multipart mail flagged for a
body the sync had not fetched — and with no detail stored, the only way to find that out
was to re-parse the mailbox by hand.

Revision ID: 0003detail
Revises: 0002choice
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003detail"
down_revision: str | Sequence[str] | None = "0002choice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message", sa.Column("parse_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("message", "parse_detail")
