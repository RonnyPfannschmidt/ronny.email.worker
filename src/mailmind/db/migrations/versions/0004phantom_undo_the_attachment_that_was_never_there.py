"""undo the attachment that was never there

A sync reads header blocks.  Read the headers of a multipart message without its body and
`email` reports the parts as missing, which the parser recorded as `partial`; `walk()` then
yields the message itself, whose content type is not text, which the parser recorded as one
attachment of type `multipart/mixed`.  Both were artefacts of the body not having been
fetched, and 0003's parser no longer produces either.

What is already cached still holds them, and no amount of syncing corrects it on its own:
an incremental sync reads only what changed on the server, and nothing changed.  Re-reading
every header would fix it at the cost of downloading the mailbox's headers again.  This
does not need to: the combination is diagnostic.  A message with no cached body cannot have
had a real attachment *and* a parse defect from headers alone — the defect is what the
missing body looked like, and the attachment is what the multipart container looked like.

Deliberately narrow.  A message flagged `partial` with no attachment may have a genuine
header defect, and one with an attachment and no defect is a single-part attachment the
headers really did describe.  Neither is touched; both are re-derived the next time a body
is fetched or a full sync runs.

Revision ID: 0004phantom
Revises: 0003detail
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004phantom"
down_revision: str | Sequence[str] | None = "0003detail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UNDO_PHANTOM = """
UPDATE message
   SET has_attachments = 0,
       parse_status = 'ok',
       parse_detail = NULL
 WHERE has_attachments = 1
   AND parse_status = 'partial'
   AND id NOT IN (SELECT message_id FROM message_body)
"""


def upgrade() -> None:
    op.execute(UNDO_PHANTOM)


def downgrade() -> None:
    # Nothing to put back: what this removed was never a fact about anybody's mail.
    pass
