"""Forgetting an account, which is the only half of managing one that lives here.

Adding belongs in the review UI — the row is the source of truth and the configuration is
seed data for it. But a seed that was wrong is a row nothing could remove: bootstrap a copy
of the example file once and `imap.example.org` is in the review UI forever.
"""

from __future__ import annotations

import sqlalchemy as sa
from click.testing import CliRunner

from mailmind.cli import main
from mailmind.config import AccountConfig, Config, Login
from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.service import TENANT_ZERO, Service

CONFIG = """
database_url = "sqlite:///{db}"

[accounts.wanted]
host = "imap.example.net"

[accounts.wanted.login]
username = "me@example.net"
password = "env://MAILMIND_TEST_PASSWORD"
"""


def seeded(tmp_path):  # noqa: ANN201
    """Two accounts in the database, one of which the configuration no longer names."""
    db = tmp_path / "mm.db"
    config = tmp_path / "mailmind.toml"
    config.write_text(CONFIG.format(db=db))
    upgrade_to_head(f"sqlite:///{db}")
    service = Service(
        Config(
            database_url=f"sqlite:///{db}",
            accounts=(
                AccountConfig(
                    name="wanted",
                    host="imap.example.net",
                    login=Login(username="me@example.net", password="env://X"),
                ),
            ),
        )
    )
    with service.scope(TENANT_ZERO) as scope:
        for name, host in (("wanted", "imap.example.net"), ("leftover", "imap.example.org")):
            account = scope.add(
                m.Account(name=name, host=host, username="u", password_url="env://X")
            )
            scope.flush()
            scope.add(m.AccountCapability(account_id=account.id, name="MOVE"))
            if name == "leftover":
                leftover_id = account.id
        scope.add(m.Producer(kind=m.ProducerKind.person, name="reviewer"))
        scope.flush()
        scope.scalar(sa.select(m.Producer)).current_account_id = leftover_id
        scope.commit()
    service.close()
    return config, f"sqlite:///{db}"


def run(config, *args):  # noqa: ANN201
    return CliRunner().invoke(main, ["--config", str(config), *args])


def test_list_says_which_account_the_configuration_no_longer_asks_for(tmp_path):
    config, _ = seeded(tmp_path)
    listed = run(config, "account", "list")
    assert listed.exit_code == 0, listed.output
    assert "not in the configuration" in listed.output
    assert "being reviewed" in listed.output, "the chosen account is worth knowing about"
    wanted = next(line for line in listed.output.splitlines() if line.startswith("wanted"))
    assert "not in the configuration" not in wanted


def test_forgetting_an_account_takes_the_preference_with_it(tmp_path):
    config, url = seeded(tmp_path)
    forgotten = run(config, "account", "forget", "leftover")
    assert forgotten.exit_code == 0, forgotten.output

    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        assert [a.name for a in scope.scalars(sa.select(m.Account))] == ["wanted"]
        # The producer stays — it is what "who accepted this" points at — and loses only
        # the preference.
        producer = scope.scalar(sa.select(m.Producer))
        assert producer is not None and producer.current_account_id is None
        assert scope.scalar(sa.select(sa.func.count()).select_from(m.AccountCapability)) == 1
        forgetting = scope.scalar(
            sa.select(m.AuditEvent).where(m.AuditEvent.verb == "account_forgotten")
        )
        assert forgetting is not None and forgetting.payload["name"] == "leftover"
    service.close()


def test_an_account_the_configuration_still_names_would_only_come_back(tmp_path):
    config, _ = seeded(tmp_path)
    refused = run(config, "account", "forget", "wanted")
    assert refused.exit_code != 0
    assert "still named in the configuration" in refused.output


def test_an_account_holding_cached_mail_is_not_forgotten_in_passing(tmp_path):
    config, url = seeded(tmp_path)
    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        account = scope.scalar(sa.select(m.Account).where(m.Account.name == "leftover"))
        scope.add(m.Container(account_id=account.id, name="INBOX", generation=1))
        scope.commit()
    service.close()

    refused = run(config, "account", "forget", "leftover")
    assert refused.exit_code != 0
    assert "cached folder" in refused.output
