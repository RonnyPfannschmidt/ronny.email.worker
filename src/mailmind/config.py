"""Configuration.

Accounts are declared here, capabilities included, because 04 wants the list of what a
backend can do written down and checked against reality rather than discovered at runtime
and hoped over.  What the file never holds is a password: only a reference to where one
is found.
"""

from __future__ import annotations

import ipaddress
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
        scheme, separator, _ = self.password.partition("://")
        if not separator or scheme not in PASSWORD_SCHEMES:
            # The value is not quoted back.  The mistake this catches is a literal
            # password in the config file, and printing it to the terminal — and into
            # whatever collects the traceback — is the one thing this must not do.
            raise ConfigError(
                f"login {self.username!r}: password must be a URL with one of "
                f"{', '.join(sorted(s + '://' for s in PASSWORD_SCHEMES))}"
                + (f", got {scheme}://" if separator else "")
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
    #: Accounts to seed the database with.  Not what a connection is built from — that is
    #: the ``account`` row, which this is bootstrapped into and which the review UI will
    #: also write.
    accounts: tuple[AccountConfig, ...] = ()
    limits: Limits = Limits()
    bind: str = "127.0.0.1"
    port: int = 8765
    #: An assertion that something in front of this process authenticates every request
    #: reaching it — a reverse proxy doing forward auth, an identity-aware proxy.  Nothing
    #: here can check that; setting it is taking responsibility for it.  Without it the
    #: service refuses to listen anywhere but loopback, because the review UI has no login.
    behind_auth_proxy: bool = False

    def account(self, name: str) -> AccountConfig:
        for account in self.accounts:
            if account.name == name:
                return account
        raise ConfigError(f"no account named {name!r} in configuration")


#: Names that are loopback without having to be resolved.  A name that is not in here is
#: not resolved either: it could resolve to anything, and the point of the check below is
#: to be sure rather than to be accommodating.
LOOPBACK_NAMES = frozenset({"localhost"})


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_loopback(host: str) -> bool:
    """Is this address reachable only from this machine?"""
    if host in LOOPBACK_NAMES:
        return True
    address = _address(host)
    return address is not None and address.is_loopback


def is_wildcard(host: str) -> bool:
    """``0.0.0.0`` or ``::`` — a bind address that is not an address anybody reaches."""
    address = _address(host)
    return address is not None and address.is_unspecified


def check_exposure(config: Config) -> None:
    """Refuse to serve an unauthenticated review UI to anything but this machine.

    The review UI has no login, and that is deliberate: on one person's own machine a
    login is ceremony protecting nothing, and the person at the keyboard is the only
    person there.  The bargain has two halves, though, and only one of them was ever
    written down — the bind address defaulted to loopback and nothing stopped it being
    changed, so ``--host 0.0.0.0`` served an accept-and-apply button to the network.

    ``behind_auth_proxy`` is the other way to hold up the same bargain: somebody else
    authenticates.  It is an assertion rather than a feature, which is why it has to be
    written down rather than inferred.
    """
    if is_loopback(config.bind) or config.behind_auth_proxy:
        return
    raise ConfigError(
        f"refusing to listen on {config.bind}: the review UI has no login, so anyone who "
        "can reach it can accept a suggestion and change somebody's mail. Bind to "
        "127.0.0.1, or put authentication in front of it and say so with "
        "behind_auth_proxy = true. See docs/11-deployment-and-identity.md."
    )


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
    try:
        limits = Limits(**limits_raw)
    except TypeError as exc:
        raise ConfigError(f"[limits]: {exc}") from exc
    return Config(
        database_url=raw.get("database_url", "sqlite:///mailmind.db"),
        accounts=accounts,
        limits=limits,
        bind=raw.get("bind", "127.0.0.1"),
        port=raw.get("port", 8765),
        behind_auth_proxy=raw.get("behind_auth_proxy", False),
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
        # Not echoed: what was passed may well be the password itself.
        raise ConfigError("password must be a URL, e.g. env://NAME")

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

    raise ConfigError(f"unknown password scheme {scheme}://")
