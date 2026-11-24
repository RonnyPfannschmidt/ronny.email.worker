"""A build whose database is behind holds the port and says so, rather than crashing.

A supervisor restarting into a checkout that moved on would otherwise loop on an exit,
with the one message that explains everything sitting in a journal nobody reads.
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from mailmind.db.migrate import downgrade_to, upgrade_to_head

CONFIG = """
database_url = "sqlite:///{db}"
bind = "127.0.0.1"
port = {port}
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as refusal:
        return refusal.code, refusal.read().decode()


def test_serve_against_a_behind_database_holds_a_page_and_exits_once_migrated(tmp_path):
    db = tmp_path / "mm.db"
    url = f"sqlite:///{db}"
    upgrade_to_head(url)
    downgrade_to(url, "0006folder")

    port = _free_port()
    config = tmp_path / "mailmind.toml"
    config.write_text(CONFIG.format(db=db, port=port))

    proc = subprocess.Popen(
        [sys.executable, "-m", "mailmind.cli", "--config", str(config), "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        status, page = 0, ""
        while time.monotonic() < deadline:
            try:
                status, page = _get(f"http://127.0.0.1:{port}/")
                break
            except OSError:
                assert proc.poll() is None, proc.stderr.read()
                time.sleep(0.1)
        assert status == 503, page
        assert "0006folder" in page and "0008index" in page, "it says which revisions"
        assert "mailmindctl migrate" in page, "and what to run"
        assert "Nothing has been touched" in page

        # Machine surfaces get the same answer, not a traceback and not silence.
        status, _ = _get(f"http://127.0.0.1:{port}/mcp/")
        assert status == 503

        # The migration runs; the holding process notices and leaves cleanly, so a
        # `Restart=always` unit comes back as the real service.
        migrated = subprocess.run(
            [sys.executable, "-m", "mailmind.cli", "--config", str(config), "migrate"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert migrated.returncode == 0, migrated.stderr
        assert proc.wait(timeout=30) == 0
        assert "exiting" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_serve_watch_exits_cleanly_when_the_source_changes(tmp_path):
    """`--watch` is exit-on-change: one restarter (the supervisor), one clean exit."""
    db = tmp_path / "mm.db"
    upgrade_to_head(f"sqlite:///{db}")
    port = _free_port()
    config = tmp_path / "mailmind.toml"
    config.write_text(CONFIG.format(db=db, port=port))
    watched = tmp_path / "src"
    watched.mkdir()
    (watched / "server.py").write_text("before\n")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mailmind.cli",
            "--config",
            str(config),
            "serve",
            "--watch",
            "--watch-path",
            str(watched),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                status, _ = _get(f"http://127.0.0.1:{port}/")
                break
            except OSError:
                assert proc.poll() is None, proc.stderr.read()
                time.sleep(0.1)
        assert status == 401, "serving (the login page), not crashed"

        (watched / "server.py").write_text("after — the edit being fixed\n")
        assert proc.wait(timeout=30) == 0, "a source change is a clean exit, not a crash"
        assert "restarts into the edit" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_serve_stops_while_somebody_has_a_page_open(tmp_path):
    """A bundle page holds an event stream for as long as its tab is open."""
    db = tmp_path / "mm.db"
    upgrade_to_head(f"sqlite:///{db}")
    port = _free_port()
    config = tmp_path / "mailmind.toml"
    config.write_text(CONFIG.format(db=db, port=port))
    runtime = tmp_path / "run"
    runtime.mkdir()

    proc = subprocess.Popen(
        [sys.executable, "-m", "mailmind.cli", "--config", str(config), "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
    )
    try:
        left = runtime / "mailmind" / f"review-{port}.link"
        deadline = time.monotonic() + 20
        status = None
        while status != 401 and time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                status, _ = _get(f"http://127.0.0.1:{port}/")
            time.sleep(0.1)
        link = re.search(r"http\S+", left.read_text()).group(0)
        browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        browser.open(link).read()
        stream = browser.open(f"http://127.0.0.1:{port}/api/events")
        assert stream.readline() == b"event: ready\n"

        proc.terminate()
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
