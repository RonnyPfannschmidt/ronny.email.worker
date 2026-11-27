"""`mailmindctl redeploy`: the order of the steps, and what is left running when one fails.

The commands, the clock and HTTP are faked; the database is real, because the idle check
is only worth as much as its reading of the tables the runner writes.
"""

from __future__ import annotations

import datetime as dt
import io
import urllib.error
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.redeploy import Host, Redeploy, RedeployFailed, checkout_of, idleness

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def database(tmp_path):  # noqa: ANN001, ANN201
    url = f"sqlite:///{tmp_path / 'mm.db'}"
    upgrade_to_head(url)
    return url


def add(url: str, *rows: object) -> None:
    engine = sa.create_engine(url)
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
    engine.dispose()


def task(status: m.TaskStatus) -> m.Task:
    return m.Task(
        tenant_id=0, kind=m.TaskKind.apply_bundle, status=status, account_id=1, subject_id=1
    )


def event(at: dt.datetime, seq: int = 1) -> m.AuditEvent:
    return m.AuditEvent(
        tenant_id=0, seq=seq, at=at, actor_kind="person", subject_kind="bundle", verb="accepted"
    )


def test_a_database_never_migrated_is_idle(tmp_path):  # noqa: ANN001
    assert idleness(f"sqlite:///{tmp_path / 'new.db'}", NOW).enough(quiet=20)


def test_an_untouched_database_is_idle(database):  # noqa: ANN001
    assert idleness(database, NOW).enough(quiet=20)


def test_a_queued_or_running_task_is_not_idle_however_quiet(database):  # noqa: ANN001
    add(database, task(m.TaskStatus.done), task(m.TaskStatus.failed))
    assert idleness(database, NOW).live_tasks == 0
    add(database, task(m.TaskStatus.running), task(m.TaskStatus.queued))
    assert idleness(database, NOW).live_tasks == 2
    assert not idleness(database, NOW).enough(quiet=0)


def test_a_recent_audit_event_is_not_idle_yet(database):  # noqa: ANN001
    add(database, event(NOW - dt.timedelta(seconds=5)))
    state = idleness(database, NOW)
    assert state.quiet_seconds == pytest.approx(5)
    assert not state.enough(quiet=20)
    assert state.enough(quiet=4)


def test_an_editable_checkout_is_found_and_a_wheel_is_not(tmp_path):  # noqa: ANN001
    package = tmp_path / "src" / "mailmind"
    package.mkdir(parents=True)
    assert checkout_of(package) is None
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    assert checkout_of(package) == tmp_path


class Response(io.BytesIO):
    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class FakeHost(Host):
    def __init__(self, *, fails: str | None = None, journal: str = "", root: int = 200) -> None:
        self.ran: list[str] = []
        self.fetched: list[str] = []
        self.fails = fails
        self.journal = journal
        self.root = root
        self.clock = 1000.0
        self.missing: set[str] = set()

    def run(self, argv, *, cwd=None, shell=False):  # noqa: ANN001, ANN201
        line = " ".join(argv)
        self.ran.append(line)
        return 1 if self.fails and self.fails in line else 0

    def output(self, argv):  # noqa: ANN001, ANN201
        line = " ".join(argv)
        self.ran.append(line)
        if argv[0] == "journalctl":
            return self.journal
        if "LoadState" in line:
            return "not-found\n" if self.fails and self.fails in line else "loaded\n"
        return "active\n"

    def which(self, name):  # noqa: ANN001, ANN201
        return None if name in self.missing else f"/usr/bin/{name}"

    def sleep(self, seconds):  # noqa: ANN001, ANN201
        self.clock += seconds

    def time(self):  # noqa: ANN201
        return self.clock

    def opener(self):  # noqa: ANN201
        def fetch(url, timeout):  # noqa: ANN001, ANN202
            self.fetched.append(url)
            if url.endswith(":8765/") and self.root != 200:
                raise urllib.error.HTTPError(url, self.root, "", {}, None)
            if url.endswith("/api/events"):
                return Response(b"event: ready\ndata: {}\n\n")
            return Response(b"{}")

        return fetch


def job(database: str, tmp_path: Path, host: FakeHost, **overrides: object) -> Redeploy:
    link = tmp_path / "review.link"
    link.write_text("http://127.0.0.1:8765/?key=secret\n")
    fields = dict(
        unit="mailmind-review",
        database_url=database,
        base_url="http://127.0.0.1:8765",
        link_file=link,
        project=tmp_path,
        config_args=("--config", "/etc/mm.toml"),
        checkout=tmp_path / "worker",
        host=host,
        say=lambda line: None,
    )
    return Redeploy(**{**fields, **overrides})


def test_the_steps_run_in_order_with_the_change_made_while_stopped(database, tmp_path):  # noqa: ANN001
    host = FakeHost()
    job(database, tmp_path, host, while_stopped="git merge --no-ff topic")()
    assert host.ran == [
        "systemctl --user show --property=LoadState --value mailmind-review",
        "systemctl --user stop mailmind-review",
        "git merge --no-ff topic",
        "npm ci --ignore-scripts",
        "npm run build",
        "uv run mailmindctl --config /etc/mm.toml migrate",
        "systemctl --user start mailmind-review",
        "systemctl --user is-active mailmind-review",
        "journalctl --user -u mailmind-review --since @1000 -o cat --no-pager",
    ]
    assert host.fetched == [
        "http://127.0.0.1:8765/",
        "http://127.0.0.1:8765/?key=secret",
        "http://127.0.0.1:8765/api/queue",
        "http://127.0.0.1:8765/api/events",
    ]


def test_an_installed_wheel_is_not_built(database, tmp_path):  # noqa: ANN001
    host = FakeHost()
    host.missing.add("npm")
    job(database, tmp_path, host, checkout=None)()
    assert not [line for line in host.ran if line.startswith("npm")]


def test_a_busy_service_is_waited_for_and_then_left_alone(database, tmp_path):  # noqa: ANN001
    add(database, task(m.TaskStatus.running))
    host = FakeHost()
    with pytest.raises(RedeployFailed, match="never idle.*nothing was stopped"):
        job(database, tmp_path, host, wait=30)()
    assert host.ran == ["systemctl --user show --property=LoadState --value mailmind-review"]
    assert host.clock >= 1030


@pytest.mark.parametrize(
    ("missing", "unit_fails"), [("npm", False), ("uv", False), (None, True)]
)
def test_what_can_be_known_beforehand_stops_nothing(database, tmp_path, missing, unit_fails):  # noqa: ANN001
    host = FakeHost(fails="LoadState" if unit_fails else None)
    if missing:
        host.missing.add(missing)
    with pytest.raises(RedeployFailed, match="nothing was stopped"):
        job(database, tmp_path, host)()
    assert not [line for line in host.ran if "stop" in line]


@pytest.mark.parametrize("failing", ["git merge", "npm run build", "migrate"])
def test_a_failure_after_the_stop_leaves_the_unit_stopped_and_says_so(
    database,
    tmp_path,
    failing,  # noqa: ANN001
):
    host = FakeHost(fails=failing)
    with pytest.raises(RedeployFailed, match="mailmind-review is stopped"):
        job(database, tmp_path, host, while_stopped="git merge topic")()
    assert "systemctl --user start mailmind-review" not in host.ran


def test_a_service_still_holding_the_port_is_a_failure(database, tmp_path):  # noqa: ANN001
    with pytest.raises(RedeployFailed, match="503.*still behind"):
        job(database, tmp_path, FakeHost(root=503))()


def test_a_login_page_counts_as_an_answer(database, tmp_path):  # noqa: ANN001
    job(database, tmp_path, FakeHost(root=401))()


def test_a_traceback_since_the_stop_is_a_failure(database, tmp_path):  # noqa: ANN001
    host = FakeHost(journal="Traceback (most recent call last):\n  ...\n")
    with pytest.raises(RedeployFailed, match="traceback"):
        job(database, tmp_path, host)()
