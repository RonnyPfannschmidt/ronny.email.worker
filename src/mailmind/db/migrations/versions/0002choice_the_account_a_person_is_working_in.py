"""the account a person is working in

The review UI's login is a key rather than an identity, so there is nobody to look up: the
reviewer is implicit, and what a person chooses instead is which account they are looking
at.  That choice is per person rather than per tenant, which is also the shape it needs
on a deployment where several authenticated people share one.

Revision ID: 0002choice
Revises: 0001initial
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002choice"
down_revision: str | Sequence[str] | None = "0001initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Raw, because neither of the two things alembic would do here works.  Batch mode drops
#: and recreates the table, and four others have a foreign key into `producer`, so it
#: cannot — the same reason 0001 renames a column rather than rebuilding.  And
#: `add_column` with a ForeignKey raises on SQLite, which has no ALTER for constraints.
#:
#: SQLite does take the whole thing in one statement, as long as the default is NULL:
#: <https://www.sqlite.org/lang_altertable.html#altertabaddcol>.  The service configures
#: no other database; a second backend would want the plain `op.add_column` here.
ADD_CURRENT_ACCOUNT = (
    "ALTER TABLE producer ADD COLUMN current_account_id INTEGER "
    "REFERENCES account (id) ON DELETE SET NULL"
)


def upgrade() -> None:
    op.execute(ADD_CURRENT_ACCOUNT)


def downgrade() -> None:
    op.drop_column("producer", "current_account_id")
