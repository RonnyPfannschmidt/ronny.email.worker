"""Seeding accounts from the configuration, and forgetting one that should not be there.

Adding an account belongs in the review UI — the row is the source of truth and the
configuration is seed data for it. What lives here is the seeding itself, the difference
between a row and the file that seeded it, and undoing a seed that was wrong: run
`account seed` against a copy of the example file once and `imap.example.org` is in the
review UI forever.
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


def test_seeding_converges_and_then_has_nothing_to_say(tmp_path):
    """Run twice, the second run is silent: seeding is something you can leave in a script
    without it telling you about a mailbox it did not change."""
    config, _ = seeded(tmp_path)
    first = run(config, "account", "seed", "--update")
    assert first.exit_code == 0, first.output
    assert first.output.strip(), "the fixture's row does not match the configuration"

    quiet = run(config, "account", "seed")
    assert quiet.exit_code == 0, quiet.output
    assert quiet.output.strip() == "", "a seed with nothing to do should say nothing"


def test_a_row_that_disagrees_with_the_configuration_is_reported_not_overwritten(tmp_path):
    """The row is what a connection is built from, so a file that has moved on is a fact
    worth stating rather than one to act on unasked. An account went on connecting to a
    host its configuration had stopped naming, and nothing anywhere said so."""
    config, url = seeded(tmp_path)
    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        row = scope.scalar(sa.select(m.Account).where(m.Account.name == "wanted"))
        row.host = "old.example"
        scope.commit()
    service.close()

    reported = run(config, "account", "seed")
    assert "host is 'old.example'" in reported.output
    assert "imap.example.net" in reported.output
    assert "--update" in reported.output

    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        assert scope.scalar(sa.select(m.Account).where(m.Account.name == "wanted")).host == (
            "old.example"
        ), "reporting is not writing"
    service.close()

    applied = run(config, "account", "seed", "--update")
    assert applied.exit_code == 0, applied.output
    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        assert scope.scalar(
            sa.select(m.Account).where(m.Account.name == "wanted")
        ).host == "imap.example.net"
    service.close()


def test_a_capability_the_configuration_dropped_stops_being_declared(tmp_path):
    """A declaration decides what the service attempts, so a stale one is not harmless.

    What it must not do is delete the row: `probe` writes what the server offered into the
    same table with `declared` false, so a row is not a declaration — the flag on it is.
    Deleting rows the file does not name threw away every capability the last probe found,
    which on a real account was thirty-three of them.
    """
    config, url = seeded(tmp_path)
    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        account = scope.scalar(sa.select(m.Account).where(m.Account.name == "wanted"))
        scope.add(m.AccountCapability(account_id=account.id, name="QRESYNC", declared=True))
        scope.add(
            m.AccountCapability(
                account_id=account.id, name="SORT", declared=False, probed_present=True
            )
        )
        scope.commit()
    service.close()

    reported = run(config, "account", "seed")
    assert "still declares QRESYNC" in reported.output
    assert "SORT" not in reported.output, "a probed capability is not a declaration"
    run(config, "account", "seed", "--update")

    service = Service(Config(database_url=url))
    with service.scope(TENANT_ZERO) as scope:
        account = scope.scalar(sa.select(m.Account).where(m.Account.name == "wanted"))
        held = {
            c.name: c
            for c in scope.scalars(
                sa.select(m.AccountCapability).where(
                    m.AccountCapability.account_id == account.id
                )
            )
        }
    assert held["QRESYNC"].declared is False, "the claim is withdrawn"
    assert held["SORT"].probed_present is True, "and what the probe learned is still there"
    service.close()
