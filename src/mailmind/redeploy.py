"""Replace a running mailmind with what is on disk now, without cutting work in half.

A unit that serves from a checkout reads its templates, built interface and migrations
from that checkout while it runs, so the moment to change the code is while it is
stopped, and the moment to stop it is when nothing is being applied. The steps live
here rather than in a shell script so the idle check reads the database through the
same models the runner writes it with.

Everything that touches the outside world goes through :class:`Host`, which a test
replaces.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

import attrs
import sqlalchemy as sa

from mailmind.db import models as m


class RedeployFailed(Exception):
    """A step did not work; the message says what state the unit was left in."""


@attrs.frozen
class Idleness:
    live_tasks: int
    #: None for a database nothing has happened in yet.
    quiet_seconds: float | None

    def enough(self, quiet: float) -> bool:
        return self.live_tasks == 0 and (
            self.quiet_seconds is None or self.quiet_seconds >= quiet
        )

    def __str__(self) -> str:
        last = (
            "no audit event yet"
            if self.quiet_seconds is None
            else f"last audit event {self.quiet_seconds:.0f}s ago"
        )
        return f"{self.live_tasks} task(s) queued or running, {last}"


def idleness(database_url: str, now: dt.datetime | None = None) -> Idleness:
    """Across every tenant: a task anyone queued is a reason to wait.

    Reads only ``task.status`` and ``audit_event.at``, so a database a migration or two
    behind the code on disk still answers: that is the usual one, since the code was
    pulled and the service is still running the old one against it.
    """
    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as connection:
            if not sa.inspect(connection).has_table(m.Task.__tablename__):
                return Idleness(0, None)  # Never migrated: nothing can be running on it.
            live = connection.scalar(
                sa.select(sa.func.count())
                .select_from(m.Task)
                .where(m.Task.status.in_([m.TaskStatus.queued, m.TaskStatus.running]))
            )
            last = connection.scalar(sa.select(sa.func.max(m.AuditEvent.at)))
    finally:
        engine.dispose()
    now = now or dt.datetime.now(dt.UTC)
    if last is not None and last.tzinfo is None:
        # SQLite hands timestamps back without the zone they were written with.
        last = last.replace(tzinfo=dt.UTC)
    return Idleness(live or 0, None if last is None else (now - last).total_seconds())


def checkout_of(package_dir: Path) -> Path | None:
    """The checkout an editable install runs from, or None for an installed wheel.

    A wheel carries the built interface; a checkout has to build it.
    """
    for root in package_dir.parents:
        if (root / "frontend" / "package.json").is_file():
            return root
    return None


class Host:
    """The real world: commands, the clock, HTTP."""

    def run(self, argv: Sequence[str], *, cwd: Path | None = None, shell: bool = False) -> int:
        command = argv[0] if shell else list(argv)
        return subprocess.run(command, cwd=cwd, shell=shell, check=False).returncode

    def output(self, argv: Sequence[str]) -> str:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False).stdout

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def time(self) -> float:
        return time.time()

    def opener(self) -> Callable[..., object]:
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)).open


@attrs.define
class Redeploy:
    unit: str
    database_url: str
    base_url: str
    link_file: Path
    #: Where ``uv run mailmindctl migrate`` runs: the deploy repository.
    project: Path
    #: Passed through to the migrate call so it reads the same configuration.
    config_args: tuple[str, ...]
    checkout: Path | None
    while_stopped: str | None = None
    quiet: float = 20.0
    wait: float = 400.0
    host: Host = attrs.field(factory=Host)
    say: Callable[[str], None] = print

    def __call__(self) -> None:
        self.preflight()
        self.until_idle()
        self.step(["systemctl", "--user", "stop", self.unit], "stopping")
        started = self.host.time()
        left = f"{self.unit} is stopped"
        if self.while_stopped:
            self.say(f"while stopped: {self.while_stopped}")
            if self.host.run([self.while_stopped], cwd=self.project, shell=True):
                raise RedeployFailed(f"the while-stopped command failed; {left}")
        if self.checkout is not None:
            frontend = self.checkout / "frontend"
            self.step(
                ["npm", "ci", "--ignore-scripts"],
                "installing the interface's packages",
                cwd=frontend,
                left=left,
            )
            self.step(
                ["npm", "run", "build"], "building the interface", cwd=frontend, left=left
            )
        self.step(
            ["uv", "run", "mailmindctl", *self.config_args, "migrate"],
            "migrating",
            cwd=self.project,
            left=left,
        )
        self.step(["systemctl", "--user", "start", self.unit], "starting", left=left)
        self.check(started)

    def preflight(self) -> None:
        """What can be found out before anything stops."""
        needed = ["systemctl", "uv"] + (["npm"] if self.checkout is not None else [])
        missing = [name for name in needed if self.host.which(name) is None]
        if missing:
            raise RedeployFailed(f"not on PATH: {', '.join(missing)}; nothing was stopped")
        loaded = self.host.output(
            ["systemctl", "--user", "show", "--property=LoadState", "--value", self.unit]
        )
        if loaded.strip() != "loaded":
            raise RedeployFailed(f"no user unit called {self.unit}; nothing was stopped")

    def until_idle(self) -> None:
        deadline = self.host.time() + self.wait
        while True:
            state = idleness(self.database_url)
            self.say(f"waiting for idle: {state}")
            if state.enough(self.quiet):
                return
            if self.host.time() >= deadline:
                raise RedeployFailed(f"never idle in {self.wait:.0f}s; nothing was stopped")
            self.host.sleep(5)

    def step(
        self, argv: list[str], what: str, *, cwd: Path | None = None, left: str = ""
    ) -> None:
        self.say(what)
        if self.host.run(argv, cwd=cwd):
            raise RedeployFailed(
                f"{what} failed: {' '.join(argv)}" + (f"; {left}" if left else "")
            )

    def check(self, started: float) -> None:
        """Up, logged into, streaming, and nothing in the journal since the stop."""
        fetch = self.host.opener()
        for _ in range(30):
            try:
                fetch(self.base_url + "/", timeout=5)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 503:
                    raise RedeployFailed(
                        f"{self.unit} answers 503: it is holding the port rather than "
                        "serving, which means the database is still behind"
                    ) from exc
                break  # Not logged in yet, which is an answer.
            except OSError:
                self.host.sleep(1)
        else:
            raise RedeployFailed(f"{self.base_url}/ never answered after the start")
        state = self.host.output(["systemctl", "--user", "is-active", self.unit]).strip()
        if state != "active":
            raise RedeployFailed(f"{self.unit} is {state or 'unknown'} after the start")
        try:
            # The link logs in: the key is traded for a cookie the opener keeps.
            fetch(self.link_file.read_text().strip(), timeout=10)
            fetch(self.base_url + "/api/queue", timeout=10)
            with fetch(self.base_url + "/api/events", timeout=10) as events:
                if not any(b"event: ready" in events.readline() for _ in range(20)):
                    raise RedeployFailed("the event stream never said ready")
        except OSError as exc:
            raise RedeployFailed(
                f"{self.unit} is up but not answering properly: {exc}"
            ) from exc
        journal = self.host.output(
            [
                "journalctl",
                "--user",
                "-u",
                self.unit,
                "--since",
                f"@{started:.0f}",
                "-o",
                "cat",
                "--no-pager",
            ]
        )
        if "Traceback" in journal:
            raise RedeployFailed(
                f"{self.unit} is up, but its journal has a traceback since the stop"
            )
        self.say(f"{self.unit} is serving {self.base_url}/")
