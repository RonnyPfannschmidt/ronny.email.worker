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


def test_a_password_url_never_contains_the_password(tmp_path):
    """What is stored and logged is a location, not a secret."""
    secret = tmp_path / "secret"
    secret.write_text("opensesame")
    login = Login(username="me", password=f"file://{secret}")
    assert "opensesame" not in login.password
    assert login.resolve() == "opensesame"
