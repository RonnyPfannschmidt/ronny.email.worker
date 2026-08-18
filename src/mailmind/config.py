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


class ConfigError(Exception):
    pass


@attrs.frozen
class AccountConfig:
    name: str
    host: str
    username: str
    secret_ref: str
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
        AccountConfig(
            name=name,
            host=body["host"],
            username=body["username"],
            secret_ref=body["secret_ref"],
            port=body.get("port", 993),
            use_ssl=body.get("use_ssl", True),
            caps=tuple(body.get("caps", DEFAULT_CAPS)),
            cache_bodies=body.get("cache_bodies", True),
        )
        for name, body in raw.get("accounts", {}).items()
    )
    limits_raw = raw.get("limits", {})
    return Config(
        database_url=raw.get("database_url", "sqlite:///mailmind.db"),
        accounts=accounts,
        limits=Limits(**limits_raw),
        bind=raw.get("bind", "127.0.0.1"),
        port=raw.get("port", 8765),
    )


def resolve_secret(secret_ref: str) -> str:
    """Turn a reference into a password.

    ``env:NAME``, ``file:/path``, or ``keyring:service/user``.  A missing secret is an
    error rather than an empty string, because an empty password reaches the server as a
    failed login and looks like something else.
    """
    scheme, _, rest = secret_ref.partition(":")
    if scheme == "env":
        value = os.environ.get(rest)
        if value is None:
            raise ConfigError(f"environment variable {rest!r} is not set")
        return value
    if scheme == "file":
        path = Path(rest).expanduser()
        if not path.exists():
            raise ConfigError(f"secret file {path} does not exist")
        return path.read_text().strip()
    if scheme == "keyring":
        service, _, user = rest.partition("/")
        import keyring  # imported here so keyring stays an optional dependency

        value = keyring.get_password(service, user)
        if value is None:
            raise ConfigError(f"no keyring entry for {service}/{user}")
        return value
    raise ConfigError(f"unknown secret reference scheme {scheme!r}")
