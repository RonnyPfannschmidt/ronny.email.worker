"""Configuration.

Accounts are declared here, capabilities included, because 04 wants the list of what a
backend can do written down and checked against reality rather than discovered at runtime
and hoped over.  What the file never holds is a password: only a reference to where one
is found.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import attrs

DEFAULT_CAPS = ("CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE")

#: Where a password may come from.  Nothing here reads a password out of the config file
#: itself, and there is deliberately no scheme that would let it.
PASSWORD_SCHEMES = frozenset({"env", "file", "secret-storage"})


class ConfigError(Exception):
    pass


@attrs.frozen
class Login:
    """How to authenticate, and where the password is found.

    ``password`` is a URL, never a password.  Its scheme says where to look:

    - ``env://NAME`` — an environment variable
    - ``file:///path/to/secret`` — a file, stripped of trailing whitespace
    - ``secret-storage://service/user`` — the desktop secret store; ``user`` may be
      omitted, in which case the login's own username is used

    A scheme this does not know is refused when the configuration loads rather than when
    a connection is attempted, because the second is somebody's mail not syncing at three
    in the morning for a reason that reads like a network fault.
    """

    username: str
    password: str

    def __attrs_post_init__(self) -> None:
        scheme = self.password.partition("://")[0]
        if scheme not in PASSWORD_SCHEMES:
            raise ConfigError(
                f"password must be a URL with one of "
                f"{', '.join(sorted(s + '://' for s in PASSWORD_SCHEMES))} — got "
                f"{self.password.split('://')[0]!r}"
            )

    def resolve(self) -> str:
        return resolve_password(self.password, username=self.username)


@attrs.frozen
class AccountConfig:
    name: str
    host: str
    login: Login
    port: int = 993
    use_ssl: bool = True
    #: What this server is declared to be able to do.  The probe checks the declaration;
    #: the declaration decides what the service attempts.  Keeping those separate is what
    #: makes a missing capability loud.
    caps: tuple[str, ...] = DEFAULT_CAPS
    cache_bodies: bool = True


@attrs.frozen
class Limits:
    #: 05: a request that would return more than this gets less, and is told so.
    max_messages_per_request: int = 200
    #: 03: suggestions nobody gets to expire rather than accumulate.
    bundle_expiry_days: int = 7
    #: How much cached body text is kept before the least recently read is evicted.
    body_cache_bytes: int = 256 * 1024 * 1024
    max_bundle_size: int = 500


@attrs.frozen
class Config:
    database_url: str = "sqlite:///mailmind.db"
    accounts: tuple[AccountConfig, ...] = ()
    limits: Limits = Limits()
    bind: str = "127.0.0.1"
    port: int = 8765

    def account(self, name: str) -> AccountConfig:
        for account in self.accounts:
            if account.name == name:
                return account
        raise ConfigError(f"no account named {name!r} in configuration")


def config_path() -> Path:
    if env := os.environ.get("MAILMIND_CONFIG"):
        return Path(env)
    local = Path.cwd() / "mailmind.toml"
    if local.exists():
        return local
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg / "mailmind" / "mailmind.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())

    accounts = tuple(
        _account(name, body) for name, body in raw.get("accounts", {}).items()
    )
    limits_raw = raw.get("limits", {})
    return Config(
        database_url=raw.get("database_url", "sqlite:///mailmind.db"),
        accounts=accounts,
        limits=Limits(**limits_raw),
        bind=raw.get("bind", "127.0.0.1"),
        port=raw.get("port", 8765),
    )


def _account(name: str, body: dict) -> AccountConfig:
    if "login" not in body:
        stray = sorted({"username", "password", "secret_ref"} & body.keys())
        hint = (
            f" — move {', '.join(stray)} into it" if stray else ""
        )
        raise ConfigError(f"account {name!r} has no [accounts.{name}.login] table{hint}")
    login = body["login"]
    for key in ("username", "password"):
        if key not in login:
            raise ConfigError(f"account {name!r}: [accounts.{name}.login] needs {key}")
    return AccountConfig(
        name=name,
        host=body["host"],
        login=Login(username=login["username"], password=login["password"]),
        port=body.get("port", 993),
        use_ssl=body.get("use_ssl", True),
        caps=tuple(body.get("caps", DEFAULT_CAPS)),
        cache_bodies=body.get("cache_bodies", True),
    )


def resolve_password(url: str, *, username: str | None = None) -> str:
    """Follow a password URL to an actual password.

    A missing secret is an error rather than an empty string: an empty password reaches
    the server as a failed login, which looks like a wrong password rather than like a
    misconfiguration.
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ConfigError(f"password must be a URL, e.g. env://NAME — got {url!r}")

    if scheme == "env":
        if not rest:
            raise ConfigError("env:// needs a variable name")
        value = os.environ.get(rest)
        if value is None:
            raise ConfigError(f"environment variable {rest!r} is not set")
        return value

    if scheme == "file":
        # Everything after the scheme is the path, so file:///abs, file://~/rel and
        # file://rel all mean what they look like.
        path = Path(rest).expanduser()
        if not path.exists():
            raise ConfigError(f"password file {path} does not exist")
        return path.read_text().strip()

    if scheme == "secret-storage":
        service, _, user = rest.partition("/")
        user = user or username
        if not service or not user:
            raise ConfigError(
                "secret-storage:// needs service/user, or a service and a login username"
            )
        try:
            import keyring  # kept an optional dependency
        except ImportError as exc:
            raise ConfigError(
                "secret-storage:// needs the keyring package installed"
            ) from exc

        value = keyring.get_password(service, user)
        if value is None:
            raise ConfigError(f"no secret-storage entry for {service}/{user}")
        return value

    raise ConfigError(f"unknown password scheme {scheme!r} in {url!r}")
