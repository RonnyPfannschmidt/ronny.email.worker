"""Configuration, and where a password comes from.

The file never holds a password. It holds a URL saying where one is found, and a scheme
this does not understand is refused when the config loads rather than at three in the
morning when a sync fails in a way that looks like a network fault.
"""

from __future__ import annotations

import pytest

from mailmind.config import (
    Config,
    ConfigError,
    Login,
    check_exposure,
    config_path,
    load_config,
    resolve_password,
)

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


def test_every_scheme_the_resolver_knows_is_a_scheme_a_login_accepts():
    """The two lists are one list. A scheme that resolves but is refused at load time is a
    documented feature nobody can turn on."""
    from mailmind.config import PASSWORD_SCHEMES

    for scheme in PASSWORD_SCHEMES:
        Login(username="me@example.org", password=f"{scheme}://somewhere")


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


#: A `pass` that does what password-store does — prints the whole entry, first line and
#: all — without needing gpg, a key, or a store on the machine running the suite. The real
#: command is exercised by hand; what this pins is what mailmind does with what it prints.
FAKE_PASS = """#!/bin/sh
[ "$1" = show ] || exit 64
shift
[ "$1" = -- ] && shift
case "$1" in
{cases}
*) echo "Error: $1 is not in the password store." >&2; exit 1 ;;
esac
"""


@pytest.fixture
def fake_pass(tmp_path, monkeypatch):
    """Put a `pass` on PATH that prints the entries a test asks for."""

    def install(entries: dict[str, str]) -> None:
        cases = "\n".join(
            f"{name}) printf '%s' \"{body}\" ;;" for name, body in entries.items()
        )
        binary = tmp_path / "bin" / "pass"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(FAKE_PASS.format(cases=cases))
        binary.chmod(0o755)
        monkeypatch.setenv("PATH", str(binary.parent), prepend=False)

    return install


def test_pass_urls_take_the_first_line_and_leave_the_notes(fake_pass):
    """password-store's whole convention: line one is the password, the rest is notes."""
    fake_pass({"mail/uberspace": "opensesame\nurl: imap.example.org\nuser: me\n"})
    assert resolve_password("pass://mail/uberspace") == "opensesame"


def test_a_pass_entry_of_one_line_survives_having_no_newline(fake_pass):
    fake_pass({"mail/plain": "opensesame"})
    assert resolve_password("pass://mail/plain") == "opensesame"


def test_a_pass_password_keeps_the_spaces_it_was_stored_with(fake_pass):
    """Unlike a file, which is stripped: what pass prints before the newline is the whole
    of the password, and a generated one can legitimately end in a space."""
    fake_pass({"mail/spaced": "open sesame \nnotes\n"})
    assert resolve_password("pass://mail/spaced") == "open sesame "


def test_an_entry_that_is_not_in_the_store_says_which(fake_pass):
    fake_pass({"mail/there": "x\n"})
    with pytest.raises(ConfigError, match="not in the password store"):
        resolve_password("pass://mail/absent")


def test_an_entry_that_begins_with_a_blank_line_is_not_an_empty_password(fake_pass):
    fake_pass({"mail/blank": "\nopensesame\n"})
    with pytest.raises(ConfigError, match="empty line"):
        resolve_password("pass://mail/blank")


def test_pass_urls_need_an_entry_and_not_an_option(fake_pass):
    fake_pass({})
    with pytest.raises(ConfigError, match="needs an entry name"):
        resolve_password("pass://")
    with pytest.raises(ConfigError, match="would be read as an option"):
        resolve_password("pass://--help")


def test_without_the_pass_command_the_scheme_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ConfigError, match="needs the pass command"):
        resolve_password("pass://mail/anything")


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


def test_binding_to_the_network_is_refused_unless_something_else_authenticates(tmp_path):
    """The setting is an assertion by the operator, not a feature.

    Nothing here can check that a proxy in front is really authenticating, which is why it
    has to be written down rather than inferred from the bind address.
    """
    # Top-level keys have to come before the first table, or TOML reads them into it.
    text = 'bind = "0.0.0.0"\n' + CONFIG
    with pytest.raises(ConfigError, match="bearer token"):
        check_exposure(load_config(write(tmp_path, text)))

    allowed = load_config(write(tmp_path, "behind_auth_proxy = true\n" + text))
    assert allowed.behind_auth_proxy is True
    check_exposure(allowed)  # says nothing, which is the point


@pytest.mark.parametrize(
    ("bind", "allowed"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),  # noqa: S104 — the case being refused
        ("::", False),
        ("192.0.2.10", False),
        # A name is not resolved: it could resolve to anything, and the check is meant to
        # be sure rather than accommodating.
        ("mail.example.org", False),
    ],
)
def test_only_addresses_that_are_provably_this_machine_serve_without_auth(bind, allowed):
    config = Config(bind=bind)
    if allowed:
        check_exposure(config)
    else:
        with pytest.raises(ConfigError):
            check_exposure(config)


def test_a_configuration_that_was_named_and_is_not_there_is_an_error(tmp_path, monkeypatch):
    """Falling back silently would give an empty configuration.

    No accounts, and a database in whatever the working directory happens to be — which,
    for a process an MCP client spawned, is not a directory anybody chose.
    """
    missing = tmp_path / "nowhere" / "mailmind.toml"
    with pytest.raises(ConfigError, match="named"):
        load_config(missing)

    monkeypatch.setenv("MAILMIND_CONFIG", str(missing))
    with pytest.raises(ConfigError, match="named"):
        load_config()

    # Nobody named anything, so an absent default is a fresh install rather than a mistake.
    monkeypatch.delenv("MAILMIND_CONFIG")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert load_config().accounts == ()


def test_a_tilde_in_the_config_path_is_expanded(tmp_path, monkeypatch):
    """These are written into MCP client configurations by hand, where no shell expands."""
    home = tmp_path / "home"
    (home / ".config" / "mailmind").mkdir(parents=True)
    written = home / ".config" / "mailmind" / "mailmind.toml"
    written.write_text(CONFIG)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MAILMIND_CONFIG", "~/.config/mailmind/mailmind.toml")
    assert config_path() == written
    assert load_config().account("personal").host == "imap.example.org"
