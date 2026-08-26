"""The stdio transport, driven the way an MCP client drives it.

A subprocess rather than an in-process call, because two of the properties asserted here
are properties of the process: that the pipe carries protocol and nothing else, and that
it opens a port only when asked to. Whether the review UI is this process or another one
is what `--serve` decides, and both halves matter — the default points at a UI that
outlives any session, and the flag is the whole setup for somebody whose agent is the only
thing that proposes anything.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import pathlib
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
import sqlalchemy as sa

from mailmind.db import models as m
from mailmind.db.engine import create_engine
from mailmind.db.migrate import upgrade_to_head
from mailmind.db.scope import make_sessionmaker, tenant_scope

CONFIG = """
database_url = "sqlite:///{db}"
bind = "127.0.0.1"
port = {port}
"""


@pytest.fixture
def workspace(tmp_path):
    """A database with one account, one folder and one message sitting in it.

    Seeded as rows rather than over IMAP: proposing touches nothing but the cache, so this
    needs no server, and it doubles as a demonstration that an account the configuration
    has never named is a working account.
    """
    db = tmp_path / "mm.db"
    url = f"sqlite:///{db}"
    upgrade_to_head(url)
    sessions = make_sessionmaker(create_engine(url))
    with tenant_scope(sessions, 0) as scope:
        account = scope.add(
            m.Account(name="dev", host="h", username="u", password_url="env://X")
        )
        scope.flush()
        inbox = scope.add(m.Container(account_id=account.id, name="INBOX", generation=1))
        archive = scope.add(m.Container(account_id=account.id, name="Archive", generation=1))
        message = scope.add(m.Message(account_id=account.id, content_key="k1", subject="Hi"))
        scope.flush()
        scope.add(
            m.Placement(
                message_id=message.id,
                container_id=inbox.id,
                uid=1,
                container_generation=1,
                seen_at=dt.datetime.now(dt.UTC),
            )
        )
        scope.commit()
        ids = {
            "account": account.id,
            "inbox": inbox.id,
            "archive": archive.id,
            "message": message.id,
        }

    # A port nothing is on, so "the UI is advertised here" and "this process is not
    # listening here" can both be asserted against the same number.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = tmp_path / "mailmind.toml"
    config.write_text(CONFIG.format(db=db, port=port))
    return {"config": config, "url": url, "sessions": sessions, "port": port, **ids}


class StdioClient:
    """Spawn the server and speak newline-delimited JSON-RPC at it."""

    def __init__(self, config, *args: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mailmind.cli", "--config", str(config), "mcp", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0
        self.lines: list[str] = []

    def startup_line(self) -> str:
        """One of the lines printed before the server starts, as a person would read them."""
        return self.proc.stderr.readline()

    def rpc(self, method: str, params: dict | None = None, *, notify: bool = False):  # noqa: ANN201
        message: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            message["id"] = self._id
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        self.lines.append(line)
        return json.loads(line)

    def call(self, name: str, **arguments):  # noqa: ANN201
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]
        assert not result.get("isError"), result["content"][0]["text"]
        structured = result.get("structuredContent")
        if structured is not None:
            return structured.get("result", structured)
        return json.loads(result["content"][0]["text"])

    def close(self) -> str:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()
        return self.proc.stderr.read()


def test_stdio_says_where_the_review_is_without_becoming_it(workspace):
    """The default: point at a `mailmindctl serve` that outlives any one session."""
    client = StdioClient(workspace["config"], "--producer", "mail-agent")
    try:
        init = client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        client.rpc("notifications/initialized", notify=True)
        instructions = init["result"]["instructions"]
        expected = f"http://127.0.0.1:{workspace['port']}/"
        assert expected in instructions, "the model was never told where to send anybody"

        assert len(client.rpc("tools/list")["result"]["tools"]) == 13

        proposed = client.call(
            "propose_bundle",
            account_id=workspace["account"],
            operation="move",
            message_ids=[workspace["message"]],
            target_container_id=workspace["archive"],
            summary="tidy",
            reason="because",
        )
        # The link travels with the proposal too: instructions are read once, and a
        # bundle is the moment somebody actually needs to go and look.
        assert proposed["note"].endswith(f"{expected}bundle/{proposed['bundle_id']}")

        # And nothing is listening there, because this process does not serve.
        with socket.socket() as probe:
            probe.settimeout(2)
            assert probe.connect_ex(("127.0.0.1", workspace["port"])) != 0, (
                "the stdio server opened a port"
            )
    finally:
        stderr = client.close()

    # Every line read was parsed as JSON on the way past; nothing may be left over either.
    leftover = [line for line in client.proc.stdout.read().splitlines() if line.strip()]
    assert leftover == [], f"something else wrote to the transport: {leftover[:2]}"
    assert "`mailmindctl serve` runs it" in stderr, "the human-facing lines go to stderr"


def test_serve_brings_the_review_ui_up_for_the_life_of_the_session(workspace):
    """The opt-in: the agent starts, and there is somewhere to review while it is running.

    For somebody whose agent is the only thing that ever proposes anything, this is the
    whole of the setup — start the agent, get told where to review, review it while the
    agent is still there.
    """
    client = StdioClient(workspace["config"], "--serve", "--port", "0")
    try:
        init = client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        client.rpc("notifications/initialized", notify=True)
        url = (
            init["result"]["instructions"].split("The person reviewing is at ")[1].split(" ")[0]
        )
        # A free port, not the configured one, which is what --port 0 is for.
        assert not url.endswith(f":{workspace['port']}/")

        proposed = client.call(
            "propose_bundle",
            account_id=workspace["account"],
            operation="move",
            message_ids=[workspace["message"]],
            target_container_id=workspace["archive"],
            summary="tidy",
            reason="because",
        )
        # What the model was told is an address, not a way in.
        with pytest.raises(urllib.error.HTTPError) as shut:
            urllib.request.urlopen(url, timeout=20)
        assert shut.value.code == 401

        # The link is left in a file, because stderr is collected into an MCP client's log
        # and some of those are put in front of the model.
        from mailmind.web.app import link_path

        left = link_path(int(url.rstrip("/").rpartition(":")[2]))
        assert left.exists(), "no link was left anywhere a person could find it"
        assert stat.S_IMODE(left.stat().st_mode) == 0o600
        opened_with = left.read_text().strip()
        assert opened_with.startswith(url) and "?key=" in opened_with

        # Following the printed link is the login, and it is a person who has it.
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with browser.open(opened_with, timeout=20) as response:
            page = response.read().decode()
        assert response.status == 200
        assert f"/bundle/{proposed['bundle_id']}" in page, "the queue did not show it"
        assert [c.name for c in jar] == ["mailmind_session"]

        # The agent is on the pipe, so the served app offers no second way in.
        with pytest.raises(urllib.error.HTTPError) as refused:
            browser.open(f"{url}mcp/", timeout=20)
        assert refused.value.code == 404

        # Ask for one more response after the web server has handled requests, so the read
        # happens where anything it wrote would be sitting in the pipe ahead of the answer.
        assert len(client.rpc("tools/list")["result"]["tools"]) == 13
    finally:
        stderr = client.close()

    leftover = [line for line in client.proc.stdout.read().splitlines() if line.strip()]
    assert leftover == [], f"something else wrote to the transport: {leftover[:2]}"
    key = opened_with.partition("?key=")[2]
    assert key and key not in stderr, "the key reached a log an MCP client collects"
    assert str(left) in stderr, "nothing said where the link was left"
    assert "sandbox the agent" in stderr, "no warning about what a local deployment is"

    # And it went away with the session.
    host, _, port = url.removeprefix("http://").rstrip("/").rpartition(":")
    with socket.socket() as probe:
        probe.settimeout(2)
        assert probe.connect_ex((host, int(port))) != 0, "the review UI outlived the session"


def test_serve_keeps_the_key_out_of_a_log_and_says_where_it_left_it(workspace):
    """`serve` prints the link for a person at a terminal. Under systemd there is none.

    A unit that sends stdout to the journal would put the login key in it, and one that
    sends stdout to /dev/null — which is what a unit written against the old behaviour does
    — left nothing at all to go on. So when stdout is not a terminal the key stays in the
    file it is written to anyway, and stderr gets the path.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "mailmind.cli", "--config", str(workspace["config"]), "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    from mailmind.web.app import link_path

    left = link_path(workspace["port"])
    try:
        deadline = time.monotonic() + 30
        while not left.exists() and time.monotonic() < deadline:  # pragma: no branch
            assert proc.poll() is None, "serve exited before it served"
            time.sleep(0.1)
        assert left.exists(), "no link was left anywhere a person could find it"
        assert stat.S_IMODE(left.stat().st_mode) == 0o600
        link = left.read_text().strip()
        key = link.partition("?key=")[2]
        assert key, "the link that was left does not open anything"
    finally:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=20)
        left.unlink(missing_ok=True)

    assert key not in stdout and key not in stderr, "the key reached something that keeps it"
    assert str(left) in stderr, "nothing said where the link was left"
    assert "mailmindctl review --open" in stderr, "nothing said how to follow it"
    assert "sandbox the agent" in stderr, "no warning about what a local deployment is"


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        (["--serve", "--review-url", "https://elsewhere.example/"], "one or the other"),
        (["--port", "0"], "only means anything with --serve"),
    ],
)
def test_arguments_that_contradict_each_other_are_refused(workspace, args, complaint):
    client = StdioClient(workspace["config"], *args)
    stderr = client.close()
    assert complaint in stderr
    assert client.proc.returncode != 0


def test_the_advertised_review_url_can_be_overridden(workspace):
    """For a UI that is not where this configuration says — behind a proxy, or elsewhere."""
    client = StdioClient(workspace["config"], "--review-url", "https://mail.example.org/review")
    try:
        init = client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        # A trailing slash is added, because the bundle links are built by appending.
        assert "https://mail.example.org/review/" in init["result"]["instructions"]
    finally:
        client.close()


def test_stdio_reuses_the_named_producer_s_grant_rather_than_widening_it(workspace):
    """`grant --producer x --capability observe` has to narrow the stdio server too."""
    with tenant_scope(workspace["sessions"], 0) as scope:
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="narrow"))
        scope.flush()
        grant = scope.add(
            m.Grant(producer_id=producer.id, token_hash="hash", capabilities=["observe"])
        )
        scope.flush()
        scope.add(m.GrantAccount(grant_id=grant.id, account_id=workspace["account"]))
        scope.commit()

    client = StdioClient(workspace["config"], "--producer", "narrow")
    try:
        client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        client.rpc("notifications/initialized", notify=True)
        assert client.call("list_accounts")[0]["name"] == "dev"

        refused = client.rpc(
            "tools/call",
            {
                "name": "propose_bundle",
                "arguments": {
                    "account_id": workspace["account"],
                    "operation": "move",
                    "message_ids": [workspace["message"]],
                    "target_container_id": workspace["archive"],
                    "summary": "s",
                    "reason": "r",
                },
            },
        )["result"]
        assert refused["isError"]
        assert "does not allow suggest" in refused["content"][0]["text"]
    finally:
        client.close()

    with tenant_scope(workspace["sessions"], 0) as scope:
        grants = scope.scalars(sa.select(m.Grant)).all()
    assert len(grants) == 1, "a second grant was minted instead of the narrow one reused"


def test_stdio_mints_a_grant_that_cannot_be_used_over_http(workspace):
    """It is minted for the record, not for anybody to hold.

    The token is generated and thrown away, so the row names a producer and covers the
    accounts without ever having been issued — which is what a grant for a process on the
    end of a pipe should be.
    """
    client = StdioClient(workspace["config"], "--producer", "fresh")
    try:
        client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        client.rpc("notifications/initialized", notify=True)
        assert [a["name"] for a in client.call("list_accounts")] == ["dev"]
    finally:
        client.close()

    with tenant_scope(workspace["sessions"], 0) as scope:
        grant = scope.scalar(
            sa.select(m.Grant).join(m.Producer).where(m.Producer.name == "fresh")
        )
        assert sorted(grant.capabilities) == ["assess", "observe", "suggest"]
        assert {ga.account_id for ga in grant.accounts} == {workspace["account"]}
        minted = scope.scalar(
            sa.select(m.AuditEvent).where(m.AuditEvent.verb == "grant_minted")
        )
        assert minted.payload["transport"] == "stdio"


#: How to get at the spawn command in each shipped configuration.
SHIPPED = {
    "opencode.json": lambda d: d["mcp"]["servers"]["mailmind"],
    "mcp-servers.json": lambda d: d["mcpServers"]["mailmind"],
}


@pytest.mark.parametrize("name", list(SHIPPED))
def test_the_shipped_client_configurations_still_match_the_command(name):
    """They get copy-pasted by people who will not read `--help` first.

    So they are parsed here rather than eyeballed: a flag renamed in the CLI fails in
    integrations/, which is where somebody would otherwise find out by having an agent
    that silently never starts.
    """
    from mailmind.cli import mcp_stdio

    path = pathlib.Path(__file__).resolve().parent.parent / "integrations" / name
    spec = SHIPPED[name](json.loads(path.read_text()))
    argv = (
        spec["command"]
        if isinstance(spec["command"], list)
        else [spec["command"], *spec["args"]]
    )

    assert argv[:2] == ["mailmindctl", "mcp"], f"{name} no longer spawns the stdio server"
    # A real parse, so an option that stopped existing is a failure rather than a warning.
    with mcp_stdio.make_context("mcp", argv[2:]) as ctx:
        assert ctx.params["serve_ui"] is True, f"{name} should bring the review UI up"
        assert ctx.params["port"] == 0, f"{name} should take a free port"
        assert ctx.params["producer"], f"{name} should name a producer"

    environment = spec.get("environment") or spec.get("env") or {}
    assert "MAILMIND_CONFIG" in environment, (
        f"{name} must name the configuration: a spawned process inherits a working "
        "directory nobody chose"
    )
    assert environment["MAILMIND_CONFIG"].startswith("~/"), (
        "the shipped path uses a tilde, which only works because config_path expands it"
    )
