"""Accounts hold a password URL rather than a secret reference.

``env:NAME`` becomes ``env://NAME``, and the column says what it holds.  The value was
never a secret and still is not — only a URL saying where one is found.

Revision ID: 0c00passurl
Revises: 0b00search
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0c00passurl"
down_revision = "0b00search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain RENAME COLUMN rather than a batch rewrite: batch mode recreates the table,
    # and every other table has a foreign key into this one, so dropping it fails.
    # SQLite has supported RENAME COLUMN since 3.25, and PostgreSQL always has.
    op.execute("ALTER TABLE account RENAME COLUMN secret_ref TO password_url")
    # env:NAME -> env://NAME, file:/path -> file:///path, keyring:x -> secret-storage://x
    for old, new in (("env:", "env://"), ("keyring:", "secret-storage://")):
        op.execute(
            sa.text(
                "UPDATE account SET password_url = :new || substr(password_url, :cut) "
                "WHERE password_url LIKE :like"
            ).bindparams(new=new, cut=len(old) + 1, like=f"{old}%")
        )
    op.execute(
        sa.text(
            "UPDATE account SET password_url = 'file://' || substr(password_url, 6) "
            "WHERE password_url LIKE 'file:%' AND password_url NOT LIKE 'file://%'"
        )
    )


def downgrade() -> None:
    op.execute("ALTER TABLE account RENAME COLUMN password_url TO secret_ref")
    # The values go back too. Renaming the column alone would leave env:// behind for a
    # parser that splits on the first colon, which would then look for an environment
    # variable called //NAME and report it missing.
    reverted = (
        ("env://", "env:"),
        ("secret-storage://", "keyring:"),
        ("file://", "file:"),
    )
    for new, old in reverted:
        op.execute(
            sa.text(
                "UPDATE account SET secret_ref = :old || substr(secret_ref, :cut) "
                "WHERE secret_ref LIKE :like"
            ).bindparams(old=old, cut=len(new) + 1, like=f"{new}%")
        )
