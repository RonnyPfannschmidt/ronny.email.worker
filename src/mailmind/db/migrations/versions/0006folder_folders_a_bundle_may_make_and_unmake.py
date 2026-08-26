"""folders a bundle may make and unmake

A move could only ever land somewhere the server already listed, because
`ck_move_needs_target` wants a `container` row and container rows came only from LIST.  So
"file these under Receipts/2026" meant stopping, making the folder by hand in a mail
client, syncing, and proposing again — and the empty folders a reorganisation leaves
behind had to be swept up by hand too, since nothing here could delete one.

Two columns on `container` and three on `suggestion` are the whole of it.

`exists_on_server` is false while a folder has been proposed and not yet made.  The row is
written when the bundle is proposed so that the target is an ordinary container everywhere
it is read — the foreign key, the review page, the unique name — and the server is only
asked for it if the person accepts.  Every row that existed before this is a folder the
server listed, so they are all true.

`discarded_at` marks a folder this service deleted.  Marked and not deleted, because
placements and suggestions still point at the row and a dangling reference tells a
reviewer nothing.

On `suggestion`, `message_id` and `premise_uid` become nullable and `premise_message_count`
appears, so that one row can carry either shape of premise: a message item remembers a UID
under a generation, a folder item remembers that the container held nothing.  The check
constraint is what stops a row being neither or both.  Reusing the row rather than adding
a table is what lets accept, exclude, reject, expire, the apply attempt and the review
table work for folders unchanged.

The `operation` enum needs no work here: it is a plain VARCHAR with no check constraint, so
`discard_container` is simply a value it did not use to hold.

Downgrading drops all five columns.  A database holding a `discard_container` bundle
should not be downgraded — the bundles survive with an operation the older code cannot
read, and their items lose the premise that said the folder was empty.

Revision ID: 0006folder
Revises: 0005consent
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006folder"
down_revision: str | Sequence[str] | None = "0005consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: A row is one shape of premise or the other, never neither and never both.
PREMISE_SHAPE = (
    "(message_id IS NOT NULL AND premise_uid IS NOT NULL) "
    "OR (message_id IS NULL AND premise_message_count IS NOT NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("container", schema=None) as batch_op:
        # NOT NULL over rows that already exist needs a default to fill them, and every
        # one of them is a folder the server listed.
        batch_op.add_column(
            sa.Column(
                "exists_on_server",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True)
        )

    # The default was for the backfill.  New rows say what they are.
    with op.batch_alter_table("container", schema=None) as batch_op:
        batch_op.alter_column("exists_on_server", server_default=None)

    with op.batch_alter_table("suggestion", schema=None) as batch_op:
        batch_op.add_column(sa.Column("premise_message_count", sa.Integer(), nullable=True))
        batch_op.alter_column("message_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("premise_uid", existing_type=sa.BigInteger(), nullable=True)
        batch_op.create_check_constraint("ck_suggestion_premise_shape", PREMISE_SHAPE)


def downgrade() -> None:
    with op.batch_alter_table("suggestion", schema=None) as batch_op:
        batch_op.drop_constraint("ck_suggestion_premise_shape", type_="check")
        batch_op.alter_column("premise_uid", existing_type=sa.BigInteger(), nullable=False)
        batch_op.alter_column("message_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("premise_message_count")

    with op.batch_alter_table("container", schema=None) as batch_op:
        batch_op.drop_column("discarded_at")
        batch_op.drop_column("exists_on_server")
