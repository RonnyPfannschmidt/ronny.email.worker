"""What a migration does to mail somebody already has cached.

Schema migrations are exercised by every test, because the suite builds its database by
running them. This is about the other kind: the ones that correct data written by an older
version of the code, where getting it wrong means quietly rewriting somebody's mailbox.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from mailmind.db.migrate import alembic_config, upgrade_to_head

#: One of each shape the old parser could leave behind, plus the one it could not.
BEFORE = [
    # A multipart message read without its body: the defect and the attachment are both
    # artefacts, and this is the combination that says so.
    ("phantom", "partial", 1, False),
    # A header defect with no attachment. Might be real; nothing here can tell.
    ("maybe-real", "partial", 0, False),
    # A single-part attachment, which headers really do describe.
    ("single-part", "ok", 1, False),
    # The same phantom shape, but a body was fetched — so the parse saw the whole message
    # and its verdict is about the message rather than about what was missing.
    ("judged", "partial", 1, True),
]


def test_the_phantom_attachment_is_undone_and_nothing_else_is(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(alembic_config(url), "0003detail")

    engine = sa.create_engine(url)
    with engine.begin() as con:
        con.execute(
            sa.text(
                "INSERT INTO account (id, name, backend, host, port, use_ssl, username, "
                "password_url, health, cache_bodies, tenant_id) VALUES "
                "(1, 'a', 'imap', 'h', 993, 1, 'u', 'env://X', 'ok', 1, 0)"
            )
        )
        for index, (name, status, attachments, body) in enumerate(BEFORE, start=1):
            con.execute(
                sa.text(
                    "INSERT INTO message (id, account_id, content_key, subject, "
                    "has_attachments, has_list_unsubscribe, parse_status, cached_at, "
                    "tenant_id) VALUES "
                    "(:id, 1, :key, :subject, :att, 0, :status, '2026-08-23', 0)"
                ),
                {
                    "id": index,
                    "key": f"k{index}",
                    "subject": name,
                    "att": attachments,
                    "status": status,
                },
            )
            if body:
                con.execute(
                    sa.text(
                        "INSERT INTO message_body (message_id, bytes_stored, links, "
                        "attachments, cached_at, last_read_at, tenant_id) VALUES "
                        "(:id, 10, '{}', '{}', '2026-08-23', '2026-08-23', 0)"
                    ),
                    {"id": index},
                )

    upgrade_to_head(url)

    with engine.begin() as con:
        after = {
            row.subject: (row.parse_status, row.has_attachments)
            for row in con.execute(
                sa.text("SELECT subject, parse_status, has_attachments FROM message")
            )
        }
    assert after["phantom"] == ("ok", 0), "the artefact should be gone"
    assert after["maybe-real"] == ("partial", 0), "a defect with no attachment is not this bug"
    assert after["single-part"] == ("ok", 1), "headers can describe a real attachment"
    assert after["judged"] == ("partial", 1), "a verdict passed on the whole message stands"
