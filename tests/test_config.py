"""Configuration, and where a password comes from.

The file never holds a password. It holds a URL saying where one is found, and a scheme
this does not understand is refused when the config loads rather than at three in the
morning when a sync fails in a way that looks like a network fault.
"""

from __future__ import annotations

import pytest

from mailmind.config import ConfigError, Login, load_config, resolve_password

CONFIG = """
database_url = "sqlite:///x.db"

[accounts.personal]
host = "imap.example.org"
port = 143
use_ssl = false
caps = ["CONDSTORE", "MOVE"]

[accounts.personal.login]
username = "me@example.org"
password = "env://PERSONAL_PASSWORD"
"""


def write(tmp_path, text):
    path = tmp_path / "mailmind.toml"
    path.write_text(text)
    return path


def test_an_account_carries_its_login_as_a_table(tmp_path):
    config = load_config(write(tmp_path, CONFIG))
    account = config.account("personal")
    assert account.host == "imap.example.org"
    assert account.port == 143
    assert account.use_ssl is False
    assert account.caps == ("CONDSTORE", "MOVE")
    assert account.login.username == "me@example.org"
    assert account.login.password == "env://PERSONAL_PASSWORD"


def test_a_login_table_is_required(tmp_path):
    text = CONFIG.replace("[accounts.personal.login]\n", "")
    with pytest.raises(ConfigError, match="no .accounts.personal.login. table"):
        load_config(write(tmp_path, text))


def test_the_old_flat_shape_says_what_to_do_about_it(tmp_path):
    text = """
[accounts.personal]
host = "imap.example.org"
username = "me@example.org"
secret_ref = "env:PERSONAL_PASSWORD"
"""
    with pytest.raises(ConfigError, match="move secret_ref, username into it"):
        load_config(write(tmp_path, text))


def test_a_password_that_is_not_a_url_is_refused_at_load_time(tmp_path):
    text = CONFIG.replace("env://PERSONAL_PASSWORD", "hunter2")
    with pytest.raises(ConfigError, match="password must be a URL"):
        load_config(write(tmp_path, text))


def test_an_unknown_scheme_is_refused_at_load_time(tmp_path):
    text = CONFIG.replace("env://", "vault://")
    with pytest.raises(ConfigError, match="password must be a URL"):
        load_config(write(tmp_path, text))


def test_a_refusal_never_quotes_back_what_might_be_the_password(tmp_path):
    """The mistake this catches is a literal password in the config file.

    Printing it to the terminal, and into whatever collects the traceback, is the one
    thing the refusal must not do.
    """
    secret = "hunter2-actual-password"  # noqa: S105 — that is the point of the test
    text = CONFIG.replace("env://PERSONAL_PASSWORD", secret)
    with pytest.raises(ConfigError) as refused:
        load_config(write(tmp_path, text))
    assert secret not in str(refused.value)

    with pytest.raises(ConfigError) as refused:
        resolve_password(secret)
    assert secret not in str(refused.value)

    # A scheme that does not exist is named; what follows it is still not.
    with pytest.raises(ConfigError) as refused:
        resolve_password(f"vault://{secret}")
    assert secret not in str(refused.value)
    assert "vault" in str(refused.value)


def test_an_unknown_key_under_limits_is_a_config_error(tmp_path):
    text = CONFIG + "\n[limits]\nmax_messages = 10\n"
    with pytest.raises(ConfigError, match="limits"):
        load_config(write(tmp_path, text))


def test_env_urls_resolve(monkeypatch):
    monkeypatch.setenv("MAILMIND_TEST_PW", "opensesame")
    assert resolve_password("env://MAILMIND_TEST_PW") == "opensesame"


def test_a_missing_environment_variable_is_an_error_not_an_empty_password(monkeypatch):
    monkeypatch.delenv("MAILMIND_ABSENT", raising=False)
    with pytest.raises(ConfigError, match="is not set"):
        resolve_password("env://MAILMIND_ABSENT")


def test_file_urls_resolve_and_lose_trailing_whitespace(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("opensesame\n")
    assert resolve_password(f"file://{secret}") == "opensesame"


def test_a_missing_password_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_password(f"file://{tmp_path / 'absent'}")


def test_secret_storage_urls_reach_the_keyring(monkeypatch):
    calls = []

    class FakeKeyring:
        @staticmethod
        def get_password(service, user):
            calls.append((service, user))
            return "opensesame"

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)
    assert resolve_password("secret-storage://imap.example.org/me") == "opensesame"
    assert calls == [("imap.example.org", "me")]


def test_secret_storage_falls_back_to_the_login_username(monkeypatch):
    calls = []

    class FakeKeyring:
        @staticmethod
        def get_password(service, user):
            calls.append((service, user))
            return "opensesame"

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)
    login = Login(username="me@example.org", password="secret-storage://imap.example.org")
    assert login.resolve() == "opensesame"
    assert calls == [("imap.example.org", "me@example.org")]


def test_secret_storage_reaches_the_real_keyring_package():
    """The test above injects a fake module into ``sys.modules``.

    That proves the scheme dispatches and proves nothing about the package it names — which
    was in no dependency list at all, so ``secret-storage://`` was documented and
    unreachable while the suite stayed green. This one imports the real thing and puts a
    real backend behind it, so the call signature is checked against the library rather
    than against our own idea of it.

    The backend is in-memory: nothing here reads or writes the developer's own store.
    """
    keyring = pytest.importorskip("keyring", reason="install the [secrets] extra")
    from keyring.backend import KeyringBackend

    class InMemory(KeyringBackend):
        priority = 1

        def __init__(self) -> None:
            self.store: dict[tuple[str, str], str] = {}

        def set_password(self, service: str, username: str, password: str) -> None:
            self.store[(service, username)] = password

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get((service, username))

        def delete_password(self, service: str, username: str) -> None:
            self.store.pop((service, username), None)

    backend = InMemory()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        backend.set_password("imap.example.org", "me@example.org", "opensesame")
        login = Login(username="me@example.org", password="secret-storage://imap.example.org")
        assert login.resolve() == "opensesame"

        missing = Login(username="nobody@example.org", password="secret-storage://nowhere")
        with pytest.raises(ConfigError, match="no secret-storage entry"):
            missing.resolve()
    finally:
        keyring.set_keyring(previous)


def test_a_password_url_never_contains_the_password(tmp_path):
    """What is stored and logged is a location, not a secret."""
    secret = tmp_path / "secret"
    secret.write_text("opensesame")
    login = Login(username="me", password=f"file://{secret}")
    assert "opensesame" not in login.password
    assert login.resolve() == "opensesame"


def test_a_command_line_override_reaches_the_configuration(tmp_path):
    """Not only the server it is handed to.

    The MCP endpoint's DNS-rebinding allow-list is built from the configured bind
    address, so ``serve --host`` that only reached uvicorn produced a service refusing
    its own Host.
    """
    from mailmind.cli import _service

    service = _service(str(write(tmp_path, CONFIG)), bind="192.0.2.10", port=9000)
    assert (service.config.bind, service.config.port) == ("192.0.2.10", 9000)
