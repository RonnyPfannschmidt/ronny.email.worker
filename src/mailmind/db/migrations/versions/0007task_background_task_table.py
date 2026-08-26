"""background task table

Bundle apply, account sync and body fetch used to run inside the request that asked for
them.  They become durable rows here, worked through by an in-process runner: `status`
and the progress columns are what a page renders, `account_id` is the lane the runner
serializes on, and `result`/`error` are what became of it.  A row in `queued` or
`running` at startup is work a crash interrupted, and the runner picks it back up.

Downgrading drops the table; queued work is lost, applied work is not — the bundle and
apply_attempt rows stay authoritative.

Revision ID: 0007task
Revises: 0006folder
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007task"
down_revision: str | Sequence[str] | None = "0006folder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "apply_bundle",
                "sync_account",
                "sync_container",
                "fetch_body",
                name="task_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="task_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("progress_done", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("progress_note", sa.String(length=255), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("producer.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_task_open", "task", ["tenant_id", "status", "account_id", "id"])
    op.create_index("ix_task_subject", "task", ["tenant_id", "kind", "subject_id"])


def downgrade() -> None:
    op.drop_index("ix_task_subject", table_name="task")
    op.drop_index("ix_task_open", table_name="task")
    op.drop_table("task")
