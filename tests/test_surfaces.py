"""The two surfaces, driven the way they are actually used.

An MCP client talks to the endpoint over HTTP with a bearer token; a person drives the
review UI with form posts.  Between them is the boundary: everything an agent can reach
is here, and none of it changes a mailbox.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

import attrs
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mailmind import views
from mailmind.config import AccountConfig, Config, ConfigError, Limits, Login
from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.imap import sync
from mailmind.imap.backend import TRASH, MailboxUnhealthy
from mailmind.imap.capabilities import probe_account
from mailmind.mcp import oauth
from mailmind.mcp import server as mcp_server
from mailmind.service import Service, hash_token
from mailmind.suggest import model as suggest
from mailmind.web import app as app_module
from mailmind.web.app import SESSION_KEY_PARAM, create_app, csrf_token, is_machine_path
from tests.corpus import CORPUS
from tests.targets.fake import FakeBackend

TOKEN = "test-token-for-opencode"


def scoped(service, work):
    """One scope's worth of async setup or assertions, on a loop of its own.

    These tests drive the app through TestClient, which brings its own loop — so the
    tests stay sync and dip into the database the same way the CLI does."""

    async def go():
        async with service.scope() as scope:
            return await work(scope)

    return service.run(go())


@pytest.fixture
def backend():
    backend = FakeBackend()
    backend.add_folder("INBOX")
    backend.add_folder("Archive", special_use="archive")
    backend.add_folder("Trash", special_use="trash")
    for raw in CORPUS.values():
        backend.add_message("INBOX", raw)
    return backend


@pytest.fixture
def service(tmp_path, backend):
    url = f"sqlite:///{tmp_path / 'mm.db'}"
    upgrade_to_head(url)
    service = Service(
        Config(
            database_url=url,
            limits=Limits(max_messages_per_request=3),
            accounts=(
                AccountConfig(
                    name="test",
                    host="h",
                    login=Login(username="u", password="env://X"),
                ),
            ),
        ),
        backend_factory=lambda _config: backend,
    )

    async def _seed(scope):
        account = scope.add(
            m.Account(name="test", host="h", username="u", password_url="env://X")
        )
        await scope.flush()
        for cap in ("CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE"):
            scope.add(m.AccountCapability(account_id=account.id, name=cap))
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        await scope.flush()
        grant = scope.add(
            m.Grant(
                producer_id=producer.id,
                token_hash=hash_token(TOKEN),
                capabilities=["observe", "suggest", "assess"],
            )
        )
        await scope.flush()
        scope.add(m.GrantAccount(grant_id=grant.id, account_id=account.id))
        await probe_account(scope, account, backend)
        for container in await sync.discover_containers(scope, account, backend):
            await sync.sync_container(scope, account, container, backend)
        await scope.commit()

    scoped(service, _seed)
    return service


#: The key `mailmindctl serve` would mint and print. Fixed here so a test can follow the
#: link the way a person does.
SESSION_KEY = "test-session-key-for-the-reviewer"

#: The form token every page derived from that key carries.
CSRF = csrf_token(SESSION_KEY)


def opened(app, base_url: str = "http://127.0.0.1:8765") -> TestClient:
    """A client that has followed the link, the way somebody starting this would.

    The cookie sticks to the client afterwards, so every test past this point is a person
    with the review UI open rather than something that found the port.
    """
    client = TestClient(app, base_url=base_url)
    client.get(f"/?{SESSION_KEY_PARAM}={SESSION_KEY}", follow_redirects=True)
    return client


@pytest.fixture
def client(service):
    # A host the MCP endpoint's rebinding protection accepts.
    app = create_app(service, session_key=SESSION_KEY)
    with opened(app) as client:
        yield client


class Agent:
    """A minimal MCP client over streamable HTTP."""

    def __init__(self, client: TestClient, token: str | None = TOKEN) -> None:
        self.client = client
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._id = 0
        self._initialise()

    def _post(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        response = self.client.post(
            "/mcp/",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": method,
                "params": params or {},
            },
        )
        if "mcp-session-id" in response.headers:
            self.headers["mcp-session-id"] = response.headers["mcp-session-id"]
        body = response.text
        for line in body.splitlines():
            if line.startswith("data: "):
                import json

                return json.loads(line[6:])
        import json

        return json.loads(body) if body else {}

    def _initialise(self) -> None:
        self._post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self.client.post(
            "/mcp/",
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    def tools(self) -> list[str]:
        return [t["name"] for t in self._post("tools/list")["result"]["tools"]]

    def call(self, name: str, **arguments) -> dict:
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        if "error" in result:
            raise AssertionError(result["error"])
        payload = result["result"]
        if payload.get("isError"):
            raise ToolRefused(payload["content"][0]["text"])
        if "structuredContent" in payload:
            content = payload["structuredContent"]
            return content.get("result", content)
        import json

        return json.loads(payload["content"][0]["text"])

    def prompts(self) -> list[str]:
        return [p["name"] for p in self._post("prompts/list")["result"]["prompts"]]

    def prompt(self, name: str, arguments: dict | None = None) -> str:
        result = self._post("prompts/get", {"name": name, "arguments": arguments or {}})
        if "error" in result:
            raise AssertionError(result["error"])
        return "".join(message["content"]["text"] for message in result["result"]["messages"])

    def read(self, uri: str):  # noqa: ANN201
        import json

        result = self._post("resources/read", {"uri": uri})
        if "error" in result:
            raise AssertionError(result["error"])
        return json.loads(result["result"]["contents"][0]["text"])


class ToolRefused(Exception):
    pass


def as_a_person(client: TestClient, path: str, **kwargs):  # noqa: ANN201
    """POST the way a page this service served does: same-origin, carrying the form token.

    The review UI refuses anything else — the session cookie authenticates, and these are
    the CSRF half. Works for a form submission and for a framework's fetch alike.
    """
    headers = {
        "Sec-Fetch-Site": "same-origin",
        "Origin": str(client.base_url).rstrip("/"),
    }
    headers.update(kwargs.pop("headers", {}))
    data = {"_csrf": CSRF}
    data.update(kwargs.pop("data", {}) or {})
    return client.post(path, headers=headers, data=data, **kwargs)


def accepting(client: TestClient, bundle_id: int, **kwargs):  # noqa: ANN201
    """Accept the way a person does: from the page, carrying what the page showed.

    The accept form sends ``reviewed_through`` so that accepting means the list that was
    read rather than whatever the bundle holds when the form arrives.  A test that posts
    without it is a test of the refusal, not of accepting.
    """
    page = client.get(f"/bundle/{bundle_id}").text
    shown = re.search(r'name="reviewed_through" value="(\d+)"', page)
    data = {"reviewed_through": shown.group(1) if shown else "0"}
    data.update(kwargs.pop("data", {}))
    response = as_a_person(
        client, f"/bundle/{bundle_id}/accept", data=data, follow_redirects=True, **kwargs
    )
    settle(client)
    return response


def settle(client: TestClient, timeout: float = 10.0) -> None:
    """Wait for the app's own runner to work the queue dry.

    Accepting enqueues; the lifespan's dispatcher applies. Tests that assert the effect
    wait for the queue rather than sleeping a guess.
    """
    import time

    service = client.app.state.task_runner.service

    async def live() -> int:
        async with service.scope() as scope:
            return await scope.scalar(
                sa.select(sa.func.count())
                .select_from(m.Task)
                .where(m.Task.status.in_((m.TaskStatus.queued, m.TaskStatus.running)))
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service.run(live()) == 0:
            return
        time.sleep(0.02)
    raise AssertionError("the task queue never went quiet")


# ------------------------------------------------------------------ the surface


#: Everything an agent can do. A new tool has to be added here consciously, which is the
#: point: this list is the agent surface, and reviewing a change to it is reviewing a
#: change to what an agent may reach.
AGENT_TOOLS = {
    "list_accounts",
    "list_containers",
    "list_messages",
    "search_messages",
    "get_message",
    "request_body",
    "summarize_senders",
    "summarize_lists",
    "request_sync",
    "task_status",
    "propose_bundle",
    "add_to_bundle",
    "propose_discard",
    "add_assessment",
    "withdraw_bundle",
}


#: Offered rather than imposed: a client that never calls prompts/get gets the same tools
#: and the same refusals. Listed here for the same reason the tools are — a change to what
#: an agent is handed is a change worth reviewing.
AGENT_PROMPTS = {"triage_mailbox", "assess_message", "hand_over"}


def test_the_prompts_carry_the_guardrails_rather_than_leaving_them_to_luck(client):
    """05 asks what an agent needs to be useful here. These are the answer so far.

    They exist because the rules that matter — start with the shape, treat content as
    data, keep a bundle readable, say where to review — are properties of how the surface
    is used, and a tool description is a bad place to put a workflow.
    """
    agent = Agent(client)
    assert set(agent.prompts()) == AGENT_PROMPTS

    for name, arguments in [
        ("triage_mailbox", {"container_id": "1"}),
        ("assess_message", {"message_id": "1"}),
        ("hand_over", {}),
    ]:
        text = agent.prompt(name, arguments)
        # Every one of them, because a client picks one and never sees the others.
        assert "cannot change this mailbox" in text
        assert "DATA" in text
        assert "not given the key" in text, name

    assert "summarize_senders" in agent.prompt("triage_mailbox", {"container_id": "1"})
    assert "Do not propose" in agent.prompt("assess_message", {"message_id": "1"})
    handover = agent.prompt("hand_over", {})
    assert "local deployment" in handover
    assert "sandboxed" in handover


def test_no_prompt_carries_the_key(client):
    """They are text the model is handed, so they are the obvious place to leak it."""
    agent = Agent(client)
    everything = "".join(
        agent.prompt(name, args)
        for name, args in [
            ("triage_mailbox", {"container_id": "1"}),
            ("assess_message", {"message_id": "1"}),
            ("hand_over", {}),
        ]
    )
    assert SESSION_KEY not in everything


def test_the_agent_surface_is_exactly_look_and_say(client):
    """Not a permission it lacks — a capability absent from this side of the service."""
    assert set(Agent(client).tools()) == AGENT_TOOLS
    assert "apply" not in [c.value for c in m.Capability]


def test_the_mcp_module_does_not_import_the_applier():
    """The applier is imported by the review flow and by nothing on the agent side.

    Checked over the parsed imports rather than the text, so a docstring mentioning the
    applier does not fail this and an import buried inside a function does.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(mcp_server.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [name for name in imported if "apply" in name], sorted(imported)


#: What a refusal for want of a grant looks like from the outside.
#:
#: Two wordings because the refusal happens in two places, depending on how the caller
#: arrived.  Over HTTP the transport rejects an unusable token before a tool runs, and says
#: ``invalid_token``; on a pipe there is no token to reject and a tool says ``no grant on
#: this request``.  Both mean the caller got no view, which is the whole of what these
#: tests are about.
NO_GRANT = ("grant", "invalid_token")


def test_without_a_token_there_is_no_view_at_all(client):
    agent = Agent(client, token=None)
    try:
        agent.call("list_accounts")
    except (ToolRefused, AssertionError) as exc:
        assert any(wording in str(exc).lower() for wording in NO_GRANT), exc
    else:
        raise AssertionError("an unauthenticated caller got a view")


def test_an_agent_sees_only_the_accounts_its_grant_covers(client, service):
    async def _work(scope):
        scope.add(m.Account(name="other", host="h", username="u2", password_url="env://Y"))
        await scope.commit()

    scoped(service, _work)
    accounts = Agent(client).call("list_accounts")
    assert [a["name"] for a in accounts] == ["test"]


#: Mail belonging to an account the grant does not cover. Distinctive enough that a leak
#: is unambiguous when one turns up in a result.
OTHER_MAIL = (
    b"From: Payroll <payroll@elsewhere.invalid>\r\n"
    b"To: me@example.org\r\n"
    b"Subject: Zorblatt salary review\r\n"
    b"Date: Wed, 19 Aug 2026 09:00:00 +0000\r\n"
    b"Message-ID: <zorblatt@elsewhere.invalid>\r\n"
    b"\r\n"
    b"Confidential.\r\n"
)


@pytest.fixture
def other_account(service):
    """A second account of the same tenant, which the grant does not cover.

    Tenancy is held below every query, so this is the boundary the loader criteria say
    nothing about: same tenant, different account, and only the grant knows the
    difference.
    """
    from mailmind.suggest import model as suggest

    elsewhere = FakeBackend()
    elsewhere.add_folder("INBOX")
    elsewhere.add_folder("Archive", special_use="archive")
    elsewhere.add_message("INBOX", OTHER_MAIL)

    async def _work(scope):
        account = scope.add(
            m.Account(name="other", host="h", username="u2", password_url="env://Y")
        )
        await scope.flush()
        containers = {
            c.name: c for c in await sync.discover_containers(scope, account, elsewhere)
        }
        for container in containers.values():
            await sync.sync_container(scope, account, container, elsewhere)
        message = await scope.scalar(
            sa.select(m.Message).where(m.Message.account_id == account.id)
        )
        producer = await scope.scalar(
            sa.select(m.Producer).where(m.Producer.name == "opencode")
        )
        bundle = await suggest.propose_bundle(
            scope,
            producer=producer,
            account=account,
            operation=m.Operation.move,
            message_ids=[message.id],
            summary="Somebody else's mail",
            reason="Should be unreachable from a grant that does not cover this account",
            target_container_id=containers["Archive"].id,
        )
        await scope.commit()
        suggestion_id = await scope.scalar(
            sa.select(m.Suggestion.id).where(m.Suggestion.bundle_id == bundle.id)
        )
        return {
            "account_id": account.id,
            "test_account_id": await scope.scalar(
                sa.select(m.Account.id).where(m.Account.name == "test")
            ),
            "container_id": containers["INBOX"].id,
            "message_id": message.id,
            "bundle_id": bundle.id,
            "suggestion_id": suggestion_id,
        }

    return scoped(service, _work)


def test_a_grant_expiry_is_honoured_rather_than_taking_the_endpoint_down(client, service):
    """An expiry used to be a TypeError, not an expiry.

    SQLite has no offset, so an aware ``expires_at`` was written without one and read back
    naive — and comparing that to an aware ``now()`` raises. It happens in the middleware,
    before any tool runs, so the first grant given an expiry would have broken every
    request rather than only its own.
    """
    assert Agent(client).call("list_accounts"), "a grant with no expiry was refused"

    def set_expiry(delta: dt.timedelta) -> None:
        async def _work(scope):
            grant = await scope.scalar(sa.select(m.Grant))
            grant.expires_at = dt.datetime.now(dt.UTC) + delta
            await scope.commit()

        scoped(service, _work)

    set_expiry(dt.timedelta(hours=1))
    assert Agent(client).call("list_accounts"), "a grant that had not expired was refused"

    set_expiry(-dt.timedelta(seconds=1))
    with pytest.raises((ToolRefused, AssertionError)) as refused:
        Agent(client).call("list_accounts")
    assert any(wording in str(refused.value).lower() for wording in NO_GRANT), refused.value


def test_an_account_outside_the_grant_is_absent_from_every_tool(client, other_account):
    """Not only from the ones that take an account id.

    ``list_accounts`` was the only place this was ever asserted, and every tool that
    takes a message or a bundle id reached straight past it: a grant covering one account
    could read another account's mail by number.
    """
    agent = Agent(client)

    for call in (
        lambda: agent.call("list_containers", account_id=other_account["account_id"]),
        lambda: agent.call("list_messages", container_id=other_account["container_id"]),
        lambda: agent.call("summarize_senders", container_id=other_account["container_id"]),
        lambda: agent.call("summarize_lists", container_id=other_account["container_id"]),
        lambda: agent.call("request_sync", container_id=other_account["container_id"]),
        lambda: agent.call("get_message", message_id=other_account["message_id"]),
        lambda: agent.call("request_body", message_id=other_account["message_id"]),
        lambda: agent.call(
            "add_assessment",
            message_id=other_account["message_id"],
            findings=[{"code": "note", "detail": "reached across the boundary"}],
        ),
        lambda: agent.call(
            "withdraw_bundle", bundle_id=other_account["bundle_id"], reason="reached"
        ),
    ):
        with pytest.raises(ToolRefused) as refused:
            call()
        assert "no " in str(refused.value)


def test_an_account_outside_the_grant_is_absent_from_search_and_resources(
    client, other_account
):
    agent = Agent(client)

    unnarrowed = agent.call("search_messages", query="Zorblatt")
    assert unnarrowed["messages"] == [], "an unnarrowed search saw another account's mail"

    with pytest.raises(AssertionError):
        agent.read(f"mailmind://bundle/{other_account['bundle_id']}")
    with pytest.raises(AssertionError):
        agent.read(f"mailmind://suggestion/{other_account['suggestion_id']}")

    open_bundles = agent.read("mailmind://bundles/open")
    assert other_account["bundle_id"] not in [b["bundle_id"] for b in open_bundles]


def test_observation_is_bounded_and_says_so(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )
    result = agent.call("list_messages", container_id=inbox["id"], limit=100)
    assert result["returned"] == 3  # the configured cap, not the 100 asked for
    assert result["truncated"] is True
    assert result["total_matching"] == len(CORPUS)
    assert "summarize_senders" in result["note"]


def test_a_date_window_cuts_where_it_says_it_does(client):
    """Bound as a bare string these were compared to SQLite's datetime text.

    ``2026-08-19T09:00:00`` sorts after ``2026-08-19 09:00:00``, so ``before`` used to
    include the message sitting exactly on the boundary instead of excluding it.
    """
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )

    def window(**kwargs):
        return agent.call("list_messages", container_id=inbox["id"], **kwargs)

    # The corpus starts at 2026-08-17 09:00Z with two messages before the 19th, which is a
    # real message sitting exactly on the boundary below. Everything else is counted
    # against the corpus rather than written down, so adding a message to it stays a
    # one-file change.
    older = 2
    boundary = "2026-08-19T09:00:00Z"
    assert window(before=boundary)["total_matching"] == older, "before must exclude its edge"
    assert window(since=boundary)["total_matching"] == len(CORPUS) - older, (
        "since must include its edge"
    )

    # A bare date means midnight, so the whole of the 19th is still to come.
    assert window(before="2026-08-19")["total_matching"] == older
    assert window(since="2026-08-19")["total_matching"] == len(CORPUS) - older

    # An offset is honoured rather than ignored: 11:00+02:00 is 09:00Z.
    assert window(before="2026-08-19T11:00:00+02:00")["total_matching"] == 2

    with pytest.raises(ToolRefused) as refused:
        window(before="last tuesday")
    assert "ISO 8601" in str(refused.value)


def test_summarising_senders_is_one_call_not_an_enumeration(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )
    summary = agent.call("summarize_senders", container_id=inbox["id"])
    # Bounded like every other observation, and by the same configured limit: this fixture
    # allows three, so what comes back is three of nine and says so.
    assert summary["total_matching"] == len(_corpus_senders())
    senders = summary["senders"]
    assert all(s["count"] >= 1 for s in senders)
    assert {s["from_address"] for s in senders} <= _corpus_senders()
    lists = agent.call("summarize_lists", container_id=inbox["id"])
    assert [entry["list_id"] for entry in lists["lists"]] == ["Weekly <weekly.list.example>"]
    assert lists["lists"][0]["has_unsubscribe"] is True


def test_a_summary_that_leaves_something_out_says_how_much(client):
    """05 again: an observation must never look complete when it is not, and summarising
    is an observation. It used to be the one that did not say."""
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )
    summary = agent.call("summarize_senders", container_id=inbox["id"], limit=2)
    assert summary["returned"] == 2
    assert summary["total_matching"] > 2
    assert summary["truncated"] is True
    assert "senders match" in summary["note"]


def _corpus_senders() -> set[str]:
    """Every distinct From in the corpus, parsed the way the cache parses it."""
    from mailmind.content.parse import parse_message

    return {parse_message(raw).from_address for raw in CORPUS.values()}


def test_a_message_arrives_marked_as_data(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )
    messages = agent.call("list_messages", container_id=inbox["id"])["messages"]
    detail = agent.call("get_message", message_id=messages[0]["message_id"])
    assert "data" in detail["content_warning"]
    assert detail["assessment"][0]["origin"] == "mechanical"


def test_search_is_served_from_the_local_cache(client):
    agent = Agent(client)
    result = agent.call("search_messages", query="Lunch")
    # Both the original and the spoofed reply to it.
    assert result["returned"] == 2
    assert all("Lunch" in msg["subject"] for msg in result["messages"])


# -------------------------------------------------------- proposing and review


def test_an_agent_can_propose_from_a_search_without_listing_the_messages_first(client):
    """The point of the whole thing: no paging a mailing list into a list of ids."""
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    proposed = agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="move",
        query="news@list.example",
        target_container_id=containers["Archive"]["id"],
        summary="Newsletter issues, never replied to",
        reason="Bulk mail with a List-Id and an unsubscribe header",
    )
    assert proposed["items"] == 1
    assert proposed["query"]["text"] == "news@list.example"
    assert proposed["query"]["matched"] == 1
    assert "Nothing has changed in the mailbox" in proposed["note"]


def test_naming_the_messages_and_a_query_at_once_is_refused_on_the_agent_surface(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    with pytest.raises(ToolRefused, match="not both"):
        agent.call(
            "propose_bundle",
            account_id=account["id"],
            operation="move",
            message_ids=[1],
            query="news@list.example",
            target_container_id=containers["Archive"]["id"],
            summary="Both at once",
            reason="Which one did it mean",
        )


def test_an_agent_told_a_query_was_narrowed_is_told_by_how_much(client, service, backend):
    """05: an observation that never looks complete when it is not — and neither does this.

    One of the two matches is already in Archive, so it is left out rather than refusing
    the bundle, and the answer says so instead of quietly returning one of two.
    """

    async def _work(scope):
        account = await scope.scalar(sa.select(m.Account))
        containers = {c.name: c for c in await scope.all(sa.select(m.Container))}
        uid = await scope.scalar(
            sa.select(m.Placement.uid)
            .join(m.Message, m.Placement.message_id == m.Message.id)
            .where(m.Message.subject == "Lunch on Thursday")
        )
        backend.out_of_band_move("INBOX", uid, "Archive")
        await sync.sync_container(scope, account, containers["INBOX"], backend)
        await sync.sync_container(scope, account, containers["Archive"], backend)
        await scope.commit()

    scoped(service, _work)

    agent = Agent(client)
    account_id = agent.call("list_accounts")[0]["id"]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account_id)}
    proposed = agent.call(
        "propose_bundle",
        account_id=account_id,
        operation="move",
        query="Lunch",
        target_container_id=containers["Archive"]["id"],
        summary="Lunch threads",
        reason="Done with",
    )
    assert proposed["query"]["matched"] == 2
    assert proposed["query"]["proposed"] == 1
    assert proposed["query"]["skipped"] == {"already_in_target": 1}
    assert "already in Archive" in proposed["query"]["note"]


def test_an_agent_growing_a_bundle_is_told_it_is_still_only_a_proposal(client):
    agent = Agent(client)
    proposed = _propose(agent)
    grown = agent.call("add_to_bundle", bundle_id=proposed["bundle_id"], query="Lunch")
    assert grown["items"] == proposed["items"] + 2
    assert grown["status"] == "proposed"
    assert grown["query"]["proposed"] == 2
    assert "Nothing has changed in the mailbox" in grown["note"]


def test_an_agent_cannot_add_to_a_bundle_another_producer_made(client, service):
    agent = Agent(client)
    proposed = _propose(agent)

    async def _work(scope):
        bundle = await scope.get(m.Bundle, proposed["bundle_id"])
        somebody_else = scope.add(m.Producer(kind=m.ProducerKind.agent, name="somebody-else"))
        await scope.flush()
        bundle.producer_id = somebody_else.id
        await scope.commit()

    scoped(service, _work)

    with pytest.raises(ToolRefused, match="only be added to by the producer"):
        agent.call("add_to_bundle", bundle_id=proposed["bundle_id"], query="Lunch")


def test_a_bundle_that_grew_cannot_be_accepted_from_the_page_drawn_before_it(client, backend):
    """The consent gap, closed end to end: the person reads, the agent adds, the accept
    refuses and says what arrived."""
    agent = Agent(client)
    proposed = _propose(agent)

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    shown = re.search(r'name="reviewed_through" value="(\d+)"', page).group(1)

    agent.call("add_to_bundle", bundle_id=proposed["bundle_id"], query="Lunch")

    refused = as_a_person(
        client,
        f"/bundle/{proposed['bundle_id']}/accept",
        data={"reviewed_through": shown},
        follow_redirects=True,
    )
    assert "2 more messages arrived" in refused.text
    assert len(backend.folders["Archive"].messages) == 0, "it applied anyway"

    # Reading it again is the whole resolution: the page now shows what arrived.
    assert "added later" in client.get(f"/bundle/{proposed['bundle_id']}").text
    accepting(client, proposed["bundle_id"])
    assert len(backend.folders["Archive"].messages) == 3


def test_the_review_page_says_a_bundle_was_found_by_searching(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    proposed = agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="move",
        query="news@list.example",
        target_container_id=containers["Archive"]["id"],
        summary="Newsletter issues",
        reason="Bulk mail",
    )
    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    assert "found by searching for" in page
    assert "the search is not run again" in page


def _propose(agent: Agent) -> dict:
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    # Named rather than summarised for: this fixture caps observation at three, so the
    # summary is legitimately truncated and the newsletter may not be in it.
    messages = agent.call(
        "list_messages",
        container_id=containers["INBOX"]["id"],
        from_address="news@list.example",
    )["messages"]
    return agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="move",
        message_ids=[msg["message_id"] for msg in messages],
        target_container_id=containers["Archive"]["id"],
        summary="Newsletter issues, never replied to",
        reason="Bulk mail with a List-Id and an unsubscribe header",
    )


def test_an_agent_can_file_into_a_folder_that_does_not_exist_yet(client, backend):
    """The folder is part of the proposal, not something done on the way to making one."""
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    messages = agent.call(
        "list_messages",
        container_id=containers["INBOX"]["id"],
        from_address="news@list.example",
    )["messages"]

    result = agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="move",
        message_ids=[msg["message_id"] for msg in messages],
        target_container_name="Lists/example",
        summary="Newsletter issues",
        reason="Bulk mail with a List-Id",
    )

    assert result["status"] == "proposed"
    assert "Nothing has changed" in result["note"]
    # The whole point: proposing a folder is not making one.
    assert "Lists/example" not in backend.folders

    listed = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    assert listed["Lists/example"]["exists_on_server"] is False
    assert listed["INBOX"]["exists_on_server"] is True


def test_an_agent_can_propose_discarding_an_empty_folder_and_nothing_happens(
    client, service, backend
):
    backend.add_folder("Old")

    async def _work(scope):
        account_row = await scope.scalar(sa.select(m.Account))
        await sync.discover_containers(scope, account_row, backend)
        await scope.commit()

    scoped(service, _work)

    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}

    result = agent.call(
        "propose_discard",
        account_id=account["id"],
        container_ids=[containers["Old"]["id"]],
        summary="One empty leftover",
        reason="Nothing has ever been filed in it",
    )

    assert result["status"] == "proposed"
    assert result["items"] == 1
    assert "Nothing has changed" in result["note"]
    assert "Old" in backend.folders

    bundle = agent.read(result["resource"])
    assert bundle["operation"] == "discard_container"
    assert bundle["items"][0]["container"] == "Old"
    assert bundle["items"][0]["message_id"] is None

    item = agent.read(f"mailmind://suggestion/{bundle['items'][0]['suggestion_id']}")
    assert item["premise"]["message_count"] == 0
    assert item["premise"]["uid"] is None


def test_an_agent_cannot_sync_a_folder_that_is_only_proposed(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    messages = agent.call(
        "list_messages",
        container_id=containers["INBOX"]["id"],
        from_address="news@list.example",
    )["messages"]
    agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="move",
        message_ids=[msg["message_id"] for msg in messages],
        target_container_name="Lists/example",
        summary="s",
        reason="r",
    )
    listed = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}

    with pytest.raises(Exception, match="nothing on the server"):
        agent.call("request_sync", container_id=listed["Lists/example"]["id"])


def test_proposing_changes_nothing_and_says_so(client, backend):
    result = _propose(Agent(client))
    assert result["status"] == "proposed"
    assert "Nothing has changed" in result["note"]
    assert len(backend.folders["Archive"].messages) == 0


def test_a_suggestion_is_readable_as_a_resource(client):
    agent = Agent(client)
    proposed = _propose(agent)

    index = agent.read("mailmind://bundles/open")
    assert [b["bundle_id"] for b in index] == [proposed["bundle_id"]]

    bundle = agent.read(proposed["resource"])
    assert bundle["operation"] == "move"
    assert bundle["items"][0]["currently_in"] == "INBOX"
    assert bundle["items"][0]["would_move_to"] == "Archive"

    suggestion = agent.read(f"mailmind://suggestion/{bundle['items'][0]['suggestion_id']}")
    assert suggestion["premise"]["uid"] > 0
    assert suggestion["status"] == "proposed"


def test_an_agent_can_record_an_assessment_but_only_as_interpretation(client, service):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )
    message = agent.call("list_messages", container_id=inbox["id"])["messages"][0]
    agent.call(
        "add_assessment",
        message_id=message["message_id"],
        findings=[{"code": "pressure", "detail": "reads as urgency", "evidence": {}}],
    )
    detail = agent.call("get_message", message_id=message["message_id"])
    classes = {f["class"] for a in detail["assessment"] for f in a["findings"]}
    assert classes == {"mechanical", "interpretation"}
    producer_findings = [
        f for a in detail["assessment"] if a["origin"] == "producer" for f in a["findings"]
    ]
    assert all(f["class"] == "interpretation" for f in producer_findings)


def test_the_review_page_shows_the_effect_and_names_the_self_assessment(client):
    agent = Agent(client)
    proposed = _propose(agent)
    bundle = agent.read(proposed["resource"])
    agent.call(
        "add_assessment",
        message_id=bundle["items"][0]["message_id"],
        findings=[{"code": "bulk", "detail": "a newsletter"}],
    )

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    assert "INBOX" in page and "Archive" in page
    assert "Issue 402" in page
    # 02's rule, stated where it cannot be enforced.
    assert "same\n  producer that proposed this" in page or "same" in page
    assert "opencode" in page


def test_the_review_page_says_a_folder_would_be_made_before_anything_moves(client, backend):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    messages = agent.call(
        "list_messages",
        container_id=containers["INBOX"]["id"],
        from_address="news@list.example",
    )["messages"]
    proposed = agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="move",
        message_ids=[msg["message_id"] for msg in messages],
        target_container_name="Lists/example",
        summary="Newsletter issues",
        reason="Bulk mail with a List-Id",
    )

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    assert "Lists/example" in page
    assert "does not exist yet" in page, "the reviewer has to be told what they are making"

    response = accepting(client, proposed["bundle_id"])
    assert response.status_code == 200
    assert "Lists/example" in backend.folders
    assert len(backend.folders["Lists/example"].messages) == len(messages)


def test_a_discard_is_reviewed_and_accepted_like_anything_else(client, service, backend):
    backend.add_folder("Old")

    async def _work(scope):
        await sync.discover_containers(scope, await scope.scalar(sa.select(m.Account)), backend)
        await scope.commit()

    scoped(service, _work)

    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    proposed = agent.call(
        "propose_discard",
        account_id=account["id"],
        container_ids=[containers["Old"]["id"]],
        summary="One empty leftover",
        reason="Nothing has ever been filed in it",
    )

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    assert "Old" in page
    assert "cannot lose mail" in page, "why an empty folder is the only one offered"
    assert "Old" in backend.folders, "reading the page changes nothing"

    response = accepting(client, proposed["bundle_id"])
    assert response.status_code == 200
    assert "Old" not in backend.folders
    assert "applied" in response.text


def test_accepting_in_the_ui_actually_moves_the_mail(client, backend):
    agent = Agent(client)
    proposed = _propose(agent)
    assert len(backend.folders["Archive"].messages) == 0

    response = accepting(client, proposed["bundle_id"])
    assert response.status_code == 200
    assert len(backend.folders["Archive"].messages) == 1
    assert "applied" in response.text


def test_accepting_a_delete_in_the_ui_files_it_in_trash(client, backend):
    """Delete had no test at all, and no test is how the two ends stopped agreeing.

    Nothing expunges: 01 says mail has no undo, so a delete is a move into Trash and the
    message is still there afterwards.
    """
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    assert containers["Trash"]["special_use"] == TRASH, (
        "the container the applier looks for is not the one the backend reported"
    )

    messages = agent.call("list_messages", container_id=containers["INBOX"]["id"])["messages"]
    proposed = agent.call(
        "propose_bundle",
        account_id=account["id"],
        operation="delete",
        message_ids=[messages[0]["message_id"]],
        summary="Junk",
        reason="Nothing here is wanted",
    )

    response = accepting(client, proposed["bundle_id"])
    assert response.status_code == 200
    assert "no Trash container is known" not in response.text
    assert len(backend.folders["Trash"].messages) == 1
    assert "applied" in response.text


def test_the_ui_refuses_to_accept_around_something_that_moved(client, backend, service):
    agent = Agent(client)
    proposed = _propose(agent)

    # The person filed it themselves, in their own mail client.
    uid = next(iter(backend.folders["INBOX"].messages))
    backend.out_of_band_move("INBOX", uid, "Archive")

    async def _work(scope):
        account = await scope.scalar(sa.select(m.Account).where(m.Account.name == "test"))
        inbox = await scope.scalar(sa.select(m.Container).where(m.Container.name == "INBOX"))
        await sync.sync_container(scope, account, inbox, backend)
        await scope.commit()

    scoped(service, _work)

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    if "moved since this was proposed" in page:
        assert "acknowledge_stale" in page
    response = accepting(client, proposed["bundle_id"])
    assert response.status_code == 200


def test_a_bundle_whose_every_message_moved_leaves_the_queue_by_itself(
    client, backend, service
):
    """The stuck bundle, from where a person meets it.

    Every message was filed by hand before the bundle was looked at. There is nothing to
    apply, so accepting cannot work; rejecting would have recorded a refusal nobody made.
    The queue used to keep showing it either way.
    """
    agent = Agent(client)
    proposed = _propose(agent)

    async def _uids(scope):
        bundle = await scope.get(m.Bundle, proposed["bundle_id"])
        return [s.premise_uid for s in bundle.suggestions]

    uids = scoped(service, _uids)
    for uid in uids:
        backend.out_of_band_move("INBOX", uid, "Trash")

    async def _sync(scope):
        account = await scope.scalar(sa.select(m.Account).where(m.Account.name == "test"))
        inbox = await scope.scalar(sa.select(m.Container).where(m.Container.name == "INBOX"))
        await sync.sync_container(scope, account, inbox, backend)
        await scope.commit()

    scoped(service, _sync)

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    assert "closed itself" in page
    assert "Accept — do this to the mailbox" not in page, "a dead bundle offers no buttons"

    async def _status(scope):
        return (await scope.get(m.Bundle, proposed["bundle_id"])).status

    assert scoped(service, _status) is m.BundleStatus.stale

    # Gone from what is waiting, and nothing was done to the mailbox on the way out.
    queue = client.get("/").text
    assert "Newsletter issues" not in queue.split("Awaiting review")[-1].split("<h2")[0]
    assert len(backend.folders["Archive"].messages) == 0


def test_a_rejection_can_carry_a_reason_and_is_as_easy_as_accepting(client, backend):
    agent = Agent(client)
    proposed = _propose(agent)
    response = as_a_person(
        client,
        f"/bundle/{proposed['bundle_id']}/reject",
        data={"reason": "I want to keep reading these"},
        follow_redirects=True,
    )
    assert "rejected" in response.text
    assert "I want to keep reading these" in response.text
    assert len(backend.folders["Archive"].messages) == 0


def test_the_queue_lists_what_is_waiting(client):
    _propose(Agent(client))
    page = client.get("/").text
    assert "Awaiting review" in page
    assert "Archive" in page


# ------------------------------------------------------- reading and filing by hand


def _inbox(client) -> int:  # noqa: ANN001
    """The folder id, found the way the page offers it rather than by asking an agent."""
    match = re.search(r'href="/mail/(\d+)">INBOX<', client.get("/mail").text)
    assert match
    return int(match.group(1))


def _picked(client, container_id: int) -> list[str]:  # noqa: ANN001
    """The message ids a person would be ticking, read off the folder page."""
    page = client.get(f"/mail/{container_id}").text
    return re.findall(r'name="message_id" value="(\d+)"', page)


def _container(service, name: str) -> int:  # noqa: ANN001
    async def work(scope):  # noqa: ANN001, ANN202
        return (await scope.scalar(sa.select(m.Container).where(m.Container.name == name))).id

    return scoped(service, work)


def test_the_mail_page_puts_inbox_first(client):
    """Sorted by name, INBOX sits under I among however many folders there are."""
    page = client.get("/mail").text
    assert page.index(">INBOX<") < page.index(">Archive<") < page.index(">Trash<")


def test_a_person_can_read_a_folder_without_an_agent_proposing_anything(client):
    """The read view is the cache, drawn for whoever holds the key.

    It exists so that filing by hand is possible at all; that it is not a second way into
    the mailbox is what the tests below are about.
    """
    page = client.get(f"/mail/{_inbox(client)}")
    assert page.status_code == 200
    # The address rather than the name attached to it, per 08.
    assert "news@list.example" in page.text
    # Bounded like every other read: it says how much of the folder it drew.
    assert "showing" in page.text


def test_reading_a_message_by_hand_marks_nothing_and_fetches_nothing_on_its_own(
    client, service
):
    """01: this is not where mail is read, and a triage view must not drift into one.

    Opening a message leaves the flags alone — looking does not touch the mailbox — and
    the body is fetched only when it is asked for, because fetching is a connection to
    somebody's server.
    """
    listed = re.findall(
        r'href="/mail/message/(\d+)"', client.get(f"/mail/{_inbox(client)}").text
    )
    assert listed
    page = client.get(f"/mail/message/{listed[0]}")
    assert page.status_code == 200
    assert "fetch the text" in page.text

    async def flags(scope):  # noqa: ANN001, ANN202
        placement = await scope.scalar(
            views.live_placements().where(m.Placement.message_id == int(listed[0]))
        )
        return placement.flags

    assert "\\Seen" not in scoped(service, flags)


def test_filing_by_hand_proposes_rather_than_moving(client, service, backend):
    """The whole of why this view is allowed to exist.

    A person picking messages produces a bundle like any other — read on the review page,
    applied by the same accept. Nothing reaches the mailbox on the way through.
    """
    inbox = _inbox(client)
    picked = _picked(client, inbox)
    assert len(picked) >= 2

    landed = as_a_person(
        client,
        f"/mail/{inbox}/file",
        data={
            "message_id": picked[:2],
            "operation": "move",
            "target_container_id": str(_container(service, "Archive")),
        },
        follow_redirects=True,
    )
    assert landed.status_code == 200
    # The review page, with the effect enumerated, and a mailbox nobody has touched.
    assert "The effect" in landed.text
    assert "picked out of INBOX" in landed.text
    assert len(backend.folders["Archive"].messages) == 0
    # Proposed by the person, and waiting like anything else.
    assert "reviewer" in client.get("/").text


def test_a_bundle_filed_by_hand_is_accepted_the_same_way_and_then_moves(
    client, service, backend
):
    inbox = _inbox(client)
    landed = as_a_person(
        client,
        f"/mail/{inbox}/file",
        data={
            "message_id": _picked(client, inbox)[:1],
            "operation": "move",
            "target_container_id": str(_container(service, "Archive")),
        },
        follow_redirects=True,
    )
    bundle_id = int(re.search(r"/bundle/(\d+)", str(landed.url)).group(1))

    accepting(client, bundle_id)
    assert len(backend.folders["Archive"].messages) == 1


def test_filing_nothing_says_so_rather_than_proposing_an_empty_bundle(client):
    inbox = _inbox(client)
    refused = as_a_person(
        client, f"/mail/{inbox}/file", data={"operation": "move"}, follow_redirects=True
    )
    assert "nothing was selected" in refused.text


def test_a_refused_hand_filing_leaves_no_half_written_bundle_behind(client, service):
    """propose_bundle writes the bundle row before it checks the messages named.

    A refusal has to take that row with it, or a folder page quietly fills the review
    queue with bundles nobody meant to propose.
    """
    inbox = _inbox(client)
    # Into the folder they are already in: refused, because an id is a claim about a
    # message and that one does not hold.
    as_a_person(
        client,
        f"/mail/{inbox}/file",
        data={
            "message_id": _picked(client, inbox)[:1],
            "operation": "move",
            "target_container_id": str(inbox),
        },
        follow_redirects=True,
    )

    async def bundles(scope):  # noqa: ANN001, ANN202
        return len(await scope.all(sa.select(m.Bundle)))

    assert scoped(service, bundles) == 0


def test_a_hand_filed_delete_names_trash_and_nothing_else(client, service, backend):
    """The destination fields are on the form whichever button was pressed.

    A delete that carried one would draw as "delete to Archive" on the review page, which
    is not what a delete does — it goes to Trash, and nothing expunges.
    """
    inbox = _inbox(client)
    landed = as_a_person(
        client,
        f"/mail/{inbox}/file",
        data={
            "message_id": _picked(client, inbox)[:1],
            "operation": "delete",
            "target_container_id": str(_container(service, "Archive")),
        },
        follow_redirects=True,
    )
    assert "Archive" not in landed.text.split("<h1", 1)[1].split("</h1>", 1)[0]

    accepting(client, int(re.search(r"/bundle/(\d+)", str(landed.url)).group(1)))
    assert len(backend.folders["Trash"].messages) == 1
    assert len(backend.folders["Archive"].messages) == 0


def test_the_mail_view_is_behind_the_key_like_every_other_page(service, backend):
    app = create_app(service, session_key=SESSION_KEY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as stranger:
        assert stranger.get("/mail").status_code == 401
    assert not is_machine_path("/mail")


def test_the_mcp_endpoint_answers_on_the_address_it_was_told_to_serve(service, backend):
    """``serve --host`` has to reach the configuration, not only uvicorn.

    The endpoint builds its DNS-rebinding allow-list out of the configured bind address,
    so a service listening somewhere its own configuration does not name refuses the very
    Host it is there to serve.
    """
    # Still loopback — anywhere else is refused outright, which the exposure tests cover.
    elsewhere = Service(
        attrs.evolve(service.config, bind="127.0.0.2", port=9000),
        backend_factory=lambda _config: backend,
    )
    app = create_app(elsewhere, session_key=SESSION_KEY)
    with opened(app, "http://127.0.0.2:9000") as moved:
        assert [a["name"] for a in Agent(moved).call("list_accounts")] == ["test"]


def test_the_queue_shows_the_account_being_worked_in_and_no_other(client, other_account):
    """Local review has no login, so the account is what a person chooses instead.

    A view rather than a boundary: the reviewer owns all of it, and a link to a bundle in
    another account still opens. What the choice decides is what the queue is *about*.
    """
    agent = Agent(client)
    mine = _propose(agent)["bundle_id"]
    theirs = other_account["bundle_id"]

    # Nothing chosen yet, so the first account by name — "other" sorts before "test".
    queue = client.get("/").text
    assert "working in" in queue
    assert f"/bundle/{theirs}" in queue
    assert f"/bundle/{mine}" not in queue

    switched = as_a_person(
        client,
        "/accounts/choose",
        data={"account_id": other_account["test_account_id"]},
        follow_redirects=True,
    )
    assert switched.status_code == 200
    assert f"/bundle/{mine}" in switched.text
    assert f"/bundle/{theirs}" not in switched.text

    # The choice is the person's and outlives the request that made it.
    assert f"/bundle/{mine}" in client.get("/").text

    # Not a boundary: the other account's bundle is still readable by its link.
    assert client.get(f"/bundle/{theirs}").status_code == 200


def test_a_chosen_account_that_goes_away_takes_the_choice_and_not_the_producer(service):
    """`ON DELETE SET NULL` on the choice.

    Removing an account should drop a preference and never a producer, because the
    producer is what "who accepted this" points at. Removing accounts is not a feature
    yet — this asserts the clause is there so that it can be.
    """

    async def _seed(scope):
        spare = scope.add(
            m.Account(name="spare", host="h", username="u3", password_url="env://Z")
        )
        person = scope.add(m.Producer(kind=m.ProducerKind.person, name="reviewer"))
        await scope.flush()
        person.current_account_id = spare.id
        await scope.commit()
        return spare.id, person.id

    spare_id, person_id = scoped(service, _seed)

    async def _remove(scope):
        await scope.execute(sa.delete(m.Account).where(m.Account.id == spare_id))
        await scope.commit()

    scoped(service, _remove)

    async def _check(scope):
        person = await scope.get(m.Producer, person_id)
        assert person is not None, "the producer went with the account"
        assert person.current_account_id is None
        # And the fallback puts the reviewer back in a real account.
        assert (await app_module.chosen_account(scope)).name == "test"

    scoped(service, _check)


def test_an_unauthenticated_review_ui_refuses_to_listen_to_the_network(service, backend):
    """Its session cookie is a bearer token and this is plain HTTP.

    The login keeps an agent on this machine out of the review UI. It is not what makes
    the review UI safe to put on a network, where anything watching the wire can take the
    cookie and replay it.
    """
    exposed = Service(
        attrs.evolve(service.config, bind="0.0.0.0"),  # noqa: S104 — the point of the test
        backend_factory=lambda _config: backend,
    )
    with pytest.raises(ConfigError) as refused:
        create_app(exposed)
    assert "bearer token" in str(refused.value)

    # Somebody else authenticating is the other way to hold up the same bargain, and it
    # has to be said out loud because nothing here can check it.  So is where people
    # actually reach the thing: a wildcard is not an address anybody connects to, and an
    # agent told to log in there would be told to log in nowhere.
    def proxied(**extra) -> Service:  # noqa: ANN003
        return Service(
            attrs.evolve(
                service.config,
                bind="0.0.0.0",  # noqa: S104 — the point of the test
                behind_auth_proxy=True,
                **extra,
            ),
            backend_factory=lambda _config: backend,
        )

    with pytest.raises(ConfigError) as unreachable:
        create_app(proxied())
    assert "public_url" in str(unreachable.value)

    app = create_app(proxied(public_url="https://mail.example.com"), session_key=SESSION_KEY)
    with opened(app) as client:
        assert client.get("/").status_code == 200


def test_an_account_that_exists_only_in_the_database_can_still_connect(service, backend):
    """Which is every account the review UI will ever add.

    Connections used to be built by looking the account's name back up in the
    configuration file, so a row the file did not mention existed and was unusable at the
    same time — and the file cannot be the source of truth for something a web form
    writes.
    """
    from mailmind.service import account_config

    async def _work(scope):
        row = scope.add(
            m.Account(
                name="never-configured",
                host="imap.invalid",
                port=1143,
                use_ssl=False,
                username="someone@example.org",
                password_url="env://NEVER_CONFIGURED",
            )
        )
        await scope.commit()
        assert row.name not in {a.name for a in service.config.accounts}
        async with service.backend(row) as opened_backend:
            assert opened_backend is backend
        return account_config(row)

    derived = scoped(service, _work)
    assert (derived.host, derived.port, derived.use_ssl) == ("imap.invalid", 1143, False)
    assert derived.login.username == "someone@example.org"
    assert derived.login.password == "env://NEVER_CONFIGURED"


def test_the_mcp_endpoint_is_advertised_at_the_path_that_answers(client):
    """The trailing slash is not decoration.

    The endpoint is mounted at `/mcp/`, so a POST to `/mcp` gets a 307 — which a client is
    entitled to follow and may not, and which is a miserable thing to debug from the other
    side. What `mailmindctl serve` prints has to be the one that answers.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    assert client.post("/mcp/", headers=headers, json=body).status_code == 200
    # follow_redirects=False, because the test client follows them by default and would
    # hide exactly the hop a real client might not make.
    redirected = client.post("/mcp", headers=headers, json=body, follow_redirects=False)
    assert redirected.status_code == 307
    assert redirected.headers["location"].endswith("/mcp/")


class DiesPartway(FakeBackend):
    """A mailbox that goes away mid-sync, the way a real one does.

    Not a mock of anything: a FakeBackend that stops answering, which is the case a first
    sync of a real mailbox has hours of exposure to.
    """

    def __init__(self, fail_on: str) -> None:
        super().__init__()
        self._fail_on = fail_on

    def select(self, container: str, *, readonly: bool = True):  # noqa: ANN201
        if container == self._fail_on:
            raise MailboxUnhealthy("the connection went away")
        return super().select(container, readonly=readonly)


def test_a_sync_that_dies_partway_keeps_the_folders_it_finished(tmp_path):
    """A first sync of a real mailbox is long, and it used to be all-or-nothing.

    One transaction around the whole account also held SQLite's write lock for the
    duration, so every other request waited on it and gave up.
    """
    url = f"sqlite:///{tmp_path / 'mm.db'}"
    upgrade_to_head(url)
    backend = DiesPartway(fail_on="Zzz")
    backend.add_folder("INBOX")
    backend.add_folder("Archive", special_use="archive")
    backend.add_folder("Zzz")
    for raw in CORPUS.values():
        backend.add_message("INBOX", raw)
    backend.add_message("Archive", CORPUS["ordinary"])

    service = Service(Config(database_url=url), backend_factory=lambda _config: backend)

    async def _seed(scope):
        account = scope.add(
            m.Account(name="real", host="h", username="u", password_url="env://X")
        )
        await scope.commit()
        return account.id

    account_id = scoped(service, _seed)

    with opened(create_app(service, session_key=SESSION_KEY)) as client:
        died = as_a_person(client, f"/accounts/{account_id}/sync")
        # The person pressed a button, so what comes back is a page, not a stack trace.
        assert died.status_code < 400
        settle(client)

    async def _check(scope):
        account = await scope.get(m.Account, account_id)
        assert account.health is m.AccountHealth.down, "the mailbox failed and nothing said so"
        assert "the connection went away" in account.health_detail
        return {
            c.name: n
            for c, n in await scope.execute(
                sa.select(m.Container, sa.func.count(m.Placement.id))
                .outerjoin(m.Placement, m.Placement.container_id == m.Container.id)
                .group_by(m.Container.id)
            )
        }

    cached = scoped(service, _check)
    assert cached["INBOX"] == len(CORPUS), "the folders that finished were thrown away"
    assert cached["Archive"] == 1
    assert cached["Zzz"] == 0


def test_a_mailbox_that_cannot_be_reached_says_so_on_the_page(tmp_path):
    """Pressing sync against a host that will not answer was an internal server error.

    The connection is made when the button is pressed, so the failure lands in the route
    rather than inside the sync — and the person who pressed it is the one who needs to
    know what happened.
    """
    url = f"sqlite:///{tmp_path / 'mm.db'}"
    upgrade_to_head(url)

    def unreachable(_config):
        raise MailboxUnhealthy(
            "cannot reach imap.example.org:993: certificate verify failed: Hostname mismatch"
        )

    service = Service(Config(database_url=url), backend_factory=unreachable)

    async def _seed(scope):
        account = scope.add(
            m.Account(
                name="real",
                host="imap.example.org",
                username="u",
                password_url="env://X",
            )
        )
        await scope.commit()
        return account.id

    account_id = scoped(service, _seed)

    with opened(create_app(service, session_key=SESSION_KEY)) as client:
        pressed = as_a_person(client, f"/accounts/{account_id}/sync", follow_redirects=True)
        assert pressed.status_code == 200, "a mailbox being down is not a bug in the request"
        settle(client)
        # The failure is the sync task's now; the page shows it where the button is.
        page = client.get("/accounts").text
        assert "Hostname mismatch" in page, "the page does not say what went wrong"

    async def _account(scope):
        return await scope.get(m.Account, account_id)

    account = scoped(service, _account)
    assert account.health is m.AccountHealth.down
    assert "cannot reach imap.example.org:993" in account.health_detail


def test_the_review_ui_is_shut_until_the_link_is_followed(service, backend):
    """A login for local too. Not a password — a key, minted at startup and shown to
    whoever started it, which is never given to the agent."""
    app = create_app(service, session_key=SESSION_KEY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as person:
        shut = person.get("/")
        assert shut.status_code == 401
        assert "you were not given that key" in shut.text
        assert person.get("/accounts").status_code == 401
        # And the obvious guess is no better than none.
        assert person.get(f"/?{SESSION_KEY_PARAM}=letmein").status_code == 401

        # Now follow the link that was printed where this was started.
        person.get(f"/?{SESSION_KEY_PARAM}={SESSION_KEY}", follow_redirects=True)
        assert person.get("/").status_code == 200
        assert person.get("/accounts").status_code == 200


def test_following_the_link_leaves_the_key_out_of_the_address(service):
    """It should stop being in the history, the title bar and anything that logs a URL."""
    app = create_app(service, session_key=SESSION_KEY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as person:
        landed = person.get(f"/?{SESSION_KEY_PARAM}={SESSION_KEY}", follow_redirects=False)
        assert landed.status_code == 303
        assert landed.headers["location"] == "/"
        assert SESSION_KEY not in landed.headers["location"]

        cookie = landed.headers["set-cookie"]
        assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()

        # Other query parameters survive the trade, since links carry them.
        landed = person.get(
            f"/bundle/1?{SESSION_KEY_PARAM}={SESSION_KEY}&error=nope", follow_redirects=False
        )
        assert landed.headers["location"] == "/bundle/1?error=nope"


def test_the_key_is_never_told_to_the_agent(client, service):
    """The asymmetry is the whole point: the agent can send a person to the review UI and
    cannot go itself."""
    agent = Agent(client)
    everything = json.dumps(
        {
            "tools": [agent.call("list_accounts")],
            "resources": [agent.read("mailmind://accounts")],
            "proposal": _propose(agent),
        }
    )
    assert SESSION_KEY not in everything

    # Including in the instructions, which is where the address does live.
    initialised = agent._post(  # noqa: SLF001
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    assert SESSION_KEY not in json.dumps(initialised)


#: Each is one header away from a browser submitting a form, plus the case that started
#: this: an agent doing the obvious thing with an address it was handed.
NOT_A_GESTURE = {
    "nothing at all": None,
    "arriving from another site": {"Sec-Fetch-Site": "cross-site"},
    "arriving from a subframe of elsewhere": {"Sec-Fetch-Site": "same-site"},
    "no fetch metadata and a foreign origin": {"Origin": "http://evil.example"},
}


@pytest.mark.parametrize("why", list(NOT_A_GESTURE))
def test_the_review_ui_refuses_a_change_that_is_not_same_origin(client, backend, why):
    """One POST to the address the agent is handed used to accept a bundle and move mail.

    The session cookie authenticates; this is the CSRF half — a change must come from a
    page this service served, said by fetch metadata a script cannot forge (or a matching
    Origin where a browser is too old to send any). See docs/security-model.md.
    """
    proposed = _propose(Agent(client))
    spoiled = NOT_A_GESTURE[why]
    headers = {}
    if spoiled is not None:
        headers = {
            "Sec-Fetch-Site": "same-origin",
            "Origin": str(client.base_url).rstrip("/"),
            **spoiled,
        }
        if "Origin" in spoiled:
            # Exercise the no-fetch-metadata fallback: the Origin alone must match.
            headers.pop("Sec-Fetch-Site")

    refused = client.post(
        f"/bundle/{proposed['bundle_id']}/accept",
        headers=headers,
        data={"_csrf": CSRF},
    )
    assert refused.status_code == 403, why
    assert "nothing here is yours to accept" in refused.text
    assert len(backend.folders["Archive"].messages) == 0, "it applied anyway"


def test_the_review_ui_refuses_a_change_without_the_form_token(client, backend):
    """Same-origin is necessary and not sufficient: the form token is the other half."""
    proposed = _propose(Agent(client))
    refused = client.post(
        f"/bundle/{proposed['bundle_id']}/accept",
        headers={
            "Sec-Fetch-Site": "same-origin",
            "Origin": str(client.base_url).rstrip("/"),
        },
    )
    assert refused.status_code == 403
    assert "token" in refused.text
    assert len(backend.folders["Archive"].messages) == 0, "it applied anyway"


def test_a_framework_fetch_from_our_own_page_gets_through(client, backend):
    """A same-origin fetch (Turbo's form submission) is a person's action, not an agent's.

    ``sec-fetch-mode: cors`` with ``sec-fetch-site: same-origin`` is exactly what a
    framework submitting a form with fetch sends; refusing it was the old gesture check,
    replaced on purpose.
    """
    proposed = _propose(Agent(client))
    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    shown = re.search(r'name="reviewed_through" value="(\d+)"', page)
    response = client.post(
        f"/bundle/{proposed['bundle_id']}/accept",
        headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},
        data={"_csrf": CSRF, "reviewed_through": shown.group(1) if shown else "0"},
    )
    assert response.status_code < 400
    settle(client)
    assert len(backend.folders["Archive"].messages) == 1


def test_a_browser_that_withholds_the_origin_is_still_a_person(client, backend):
    """Chrome sends `Origin: null` for a same-origin form POST under some referrer
    policies — ours was one — and the UI refused every button in itself.

    The fetch metadata is what asserts a browser is showing a page to somebody. A missing
    or opaque origin is not evidence against that, and treating it as evidence locked the
    person out of their own mail.
    """
    proposed = _propose(Agent(client))
    accepted = accepting(client, proposed["bundle_id"], headers={"Origin": "null"})
    assert accepted.status_code < 400
    assert len(backend.folders["Archive"].messages) == 1, "the accept did not apply"


def test_the_referrer_policy_leaves_the_origin_alone(client):
    """The header that caused the above, pinned so it cannot come back.

    A browser derives a form POST's Origin from the referrer policy, so `no-referrer`
    means `Origin: null` on the service's own buttons. `same-origin` still sends a
    referrer to nobody but us, which is the part that matters with strangers' links on
    the page.
    """
    policy = client.get("/").headers["referrer-policy"]
    assert policy == "same-origin", "no-referrer nulls the origin of our own form posts"


def test_a_refused_change_leaves_a_mark(client, service):
    """The refusal is the interesting half: nothing changed, but something tried."""
    proposed = _propose(Agent(client))
    assert client.post(f"/bundle/{proposed['bundle_id']}/accept").status_code == 403

    async def _work(scope):
        return await scope.scalar(
            sa.select(m.AuditEvent).where(m.AuditEvent.verb == "ui_change_refused")
        )

    event = scoped(service, _work)
    assert event is not None, "a refused change should be findable afterwards"
    assert event.payload["path"].endswith("/accept")
    assert "origin" in event.payload["problem"]


def test_a_person_at_a_browser_still_gets_through(client, backend):
    """The other half: the check has to let the actual case work."""
    proposed = _propose(Agent(client))
    response = accepting(client, proposed["bundle_id"])
    assert response.status_code == 200
    assert len(backend.folders["Archive"].messages) == 1


def test_the_review_pages_forbid_remote_content(client):
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "img-src 'none'" in csp
    assert "default-src 'none'" in csp
    # The one script is our own vendored file; nothing inline, nothing remote.
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp.replace("style-src 'self' 'unsafe-inline'", "")


def test_the_script_is_served_and_stays_behind_the_key(client, service):
    """/static is a page asset, not a machine path — no session, no script."""
    served = client.get("/static/turbo.js")
    assert served.status_code == 200
    assert "javascript" in served.headers["content-type"]

    app = create_app(service, session_key=SESSION_KEY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as stranger:
        assert stranger.get("/static/turbo.js").status_code == 401


# =====================================================================================
# Logging an agent in
#
# Not what an agent may do once it holds a grant, but how it comes by one without anybody
# copying a token out of a terminal.  The flow is the ordinary OAuth one, driven the way a
# real client drives it.  What is particular to mailmind is the middle: the page where
# somebody decides is a page in the review UI, guarded by the same key as every other page,
# and what the agent asked for is not what it gets.
# =====================================================================================

REDIRECT = "http://127.0.0.1:9999/oauth/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(client, name: str = "OpenCode") -> str:
    """What a client does when it has never been here before."""
    response = client.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": name,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def ask(client, client_id: str, challenge: str) -> str:
    """Start the flow, and return the consent request it parks."""
    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "opaque-to-us",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303, 307), response.text
    location = response.headers["location"]
    assert location.startswith("/consent?"), location
    return parse_qs(urlparse(location).query)["request"][0]


def agree(client, request_id: str, *, capabilities=("observe", "suggest"), accounts=(1,)):
    """Somebody at the browser ticking boxes and pressing allow."""
    response = as_a_person(
        client,
        "/consent",
        data={
            "request_id": request_id,
            "decision": "allow",
            "capabilities": list(capabilities),
            "account_ids": [str(a) for a in accounts],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    handed_back = parse_qs(urlparse(response.headers["location"]).query)
    assert handed_back["state"] == ["opaque-to-us"]
    return handed_back["code"][0]


def redeem(client, client_id: str, code: str, verifier: str) -> dict:
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def log_in(client, **agreed) -> dict:
    """The whole walk, for tests that are about what happens afterwards."""
    verifier, challenge = pkce()
    client_id = register(client)
    request_id = ask(client, client_id, challenge)
    code = agree(client, request_id, **agreed)
    return {"client_id": client_id, **redeem(client, client_id, code, verifier)}


# --------------------------------------------------------------- what is behind the key


def test_the_page_where_somebody_decides_is_not_a_machine_endpoint():
    """The one page in this flow a person uses is guarded like every other page.

    Everything else here is a client talking to a service and has no key to offer, which
    is why the exemption exists at all. Writing it as a list rather than a prefix is what
    makes this assertable — a future endpoint is exempt by being added, not by accident.
    """
    for machine in ("/authorize", "/token", "/register", "/revoke"):
        assert is_machine_path(machine), machine
    assert is_machine_path("/.well-known/oauth-authorization-server")
    assert is_machine_path("/mcp/")

    for guarded in ("/consent", "/agents", "/agents/1/revoke", "/", "/accounts"):
        assert not is_machine_path(guarded), guarded


def test_the_consent_page_says_what_it_is_agreeing_to(client):
    """The page a person actually reads, actually rendered.

    Worth its own test because every other test here walks straight past it: the flow can
    be driven to a token without the template ever being built, so a broken page fails
    only in front of somebody.
    """
    _, challenge = pkce()
    request_id = ask(client, register(client), challenge)

    page = client.get(f"/consent?request={request_id}")
    assert page.status_code == 200

    # The name it chose for itself, and the fact that it chose it.
    assert "OpenCode" in page.text
    assert "Nothing checked it" in page.text
    # Everything a grant can hold is offered, and the account it would cover is named.
    for capability in m.Capability:
        assert f'value="{capability.value}"' in page.text
    assert "test" in page.text
    # There is no apply to tick, and the page says why.
    assert "no <em>apply</em> to tick" in page.text
    # Capabilities are pre-ticked; the mail itself is not — start narrow.
    import re as _re

    for box in _re.findall(r'<input type="checkbox"[^>]*name="capabilities"[^>]*>', page.text):
        assert "checked" in box
    for box in _re.findall(r'<input type="checkbox"[^>]*name="account_ids"[^>]*>', page.text):
        assert "checked" not in box


def test_the_shut_page_names_the_command_that_reopens_it(service):
    """Locked out (a restart, a strict cookie) the person is told the way back in."""
    app = create_app(service, session_key=SESSION_KEY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as stranger:
        shut = stranger.get("/")
    assert shut.status_code == 401
    assert "mailmindctl review --open" in shut.text


def test_excluding_lands_back_at_the_row_it_removed(client):
    """A long table reviewed item by item must not snap to the top per click."""
    proposed = _propose(Agent(client))
    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    form = re.search(r"/bundle/\d+/exclude/(\d+)", page)
    suggestion_id = form.group(1)
    response = as_a_person(
        client,
        f"/bundle/{proposed['bundle_id']}/exclude/{suggestion_id}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "#m" in response.headers["location"] or "#s" in response.headers["location"]


def test_an_agent_that_follows_its_own_link_is_told_to_fetch_a_person(service):
    """The consent page is not a page an agent can answer for itself."""
    from fastapi.testclient import TestClient

    from mailmind.web.app import create_app

    # No cookie: this client never followed the link a person was shown.
    stranger = TestClient(create_app(service, session_key="unused"))
    refused = stranger.get("/consent?request=whatever")
    assert refused.status_code == 401
    assert "not given that key" in refused.text


# ------------------------------------------------------------------------- discovery


def test_an_unauthenticated_call_says_where_to_log_in(client):
    """The 401 is the whole of how a client finds out it can log in.

    Before this, an agent with no token got a 200 and a refusal from inside a tool, which
    tells a client nothing it can act on.
    """
    response = client.post(
        "/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert "resource_metadata=" in challenge

    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"].endswith("/mcp")
    served_by = metadata["authorization_servers"][0].rstrip("/")
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    assert served_by


# ------------------------------------------------------------------------ the walk


def test_an_agent_logs_in_and_gets_the_grant_it_was_given(client):
    issued = log_in(client)
    assert issued["token_type"] == "Bearer"
    assert issued["refresh_token"]

    agent = Agent(client, token=issued["access_token"])
    assert [a["name"] for a in agent.call("list_accounts")] == ["test"]


def test_what_the_agent_gets_is_what_the_person_ticked(client):
    """The agent does not ask for capabilities, so it cannot ask for too many.

    Untick `suggest` and proposing is not a thing that grant can do — and it fails saying
    so, which is a legible thing for a model to read.
    """
    issued = log_in(client, capabilities=("observe",))
    agent = Agent(client, token=issued["access_token"])

    assert agent.call("list_accounts"), "observing was ticked and did not work"
    with pytest.raises(ToolRefused) as refused:
        agent.call(
            "propose_bundle",
            account_id=1,
            operation="delete",
            message_ids=[1],
            summary="one message",
            reason="the capability check should come first",
        )
    assert "suggest" in str(refused.value)


def test_allowing_with_no_mail_ticked_bounces_back_to_the_page(client, service):
    """Allow with nothing ticked is almost always a mistake, so the page asks again.

    No accounts on a grant still means no mail rather than all of it — the command line
    can mint such a grant — but the consent page will not create one silently.
    """
    verifier, challenge = pkce()
    client_id = register(client)
    request_id = ask(client, client_id, challenge)

    response = as_a_person(
        client,
        "/consent",
        data={
            "request_id": request_id,
            "decision": "allow",
            "capabilities": ["observe", "suggest"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    bounced = urlparse(response.headers["location"])
    assert bounced.path == "/consent", "no grant, back to the page"
    assert "You ticked no mail" in parse_qs(bounced.query)["error"][0]

    # The request is still answerable — ticking an account now completes the login.
    code = agree(client, request_id, accounts=(1,))
    assert redeem(client, client_id, code, verifier)["access_token"]


def test_refusing_hands_the_client_an_answer_rather_than_silence(client):
    verifier, challenge = pkce()
    client_id = register(client)
    request_id = ask(client, client_id, challenge)

    response = as_a_person(
        client,
        "/consent",
        data={"request_id": request_id, "decision": "refuse"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "access_denied" in response.headers["location"]


def test_a_code_is_redeemable_once(client):
    verifier, challenge = pkce()
    client_id = register(client)
    request_id = ask(client, client_id, challenge)
    code = agree(client, request_id)

    assert redeem(client, client_id, code, verifier)["access_token"]
    again = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400, again.text


# ---------------------------------------------------------------------- afterwards


def test_refreshing_rotates_and_the_old_one_stops_working(client):
    issued = log_in(client)
    first = issued["refresh_token"]

    rotated = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first,
            "client_id": issued["client_id"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["refresh_token"] != first

    # The new access token works and the spent refresh token does not.
    assert Agent(client, token=second["access_token"]).call("list_accounts")
    replayed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first,
            "client_id": issued["client_id"],
        },
    )
    assert replayed.status_code == 400, replayed.text


def test_refreshing_does_not_multiply_what_a_person_agreed_to(client, service):
    """Tokens rotate; the decision does not.

    This is the reason a token is not a grant. If it were, an hourly refresh would leave
    the agents page listing a fresh consent every hour and "what did I agree to" would
    stop being answerable.
    """
    issued = log_in(client)

    async def _count(scope):
        return len(await scope.all(sa.select(m.Grant)))

    before = scoped(service, _count)

    for _ in range(3):
        issued = {
            "client_id": issued["client_id"],
            **client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": issued["refresh_token"],
                    "client_id": issued["client_id"],
                },
            ).json(),
        }

    assert scoped(service, _count) == before


def test_revoking_on_the_agents_page_stops_a_token_that_was_working(client, service):
    issued = log_in(client)
    agent = Agent(client, token=issued["access_token"])
    assert agent.call("list_accounts"), "the grant did not work before it was revoked"

    listed = client.get("/agents")
    assert "OpenCode" in listed.text

    async def _work(scope):
        grant = await scope.scalar(sa.select(m.Grant).where(m.Grant.client_id.is_not(None)))
        return grant.id

    grant_id = scoped(service, _work)
    as_a_person(client, f"/agents/{grant_id}/revoke")

    with pytest.raises((ToolRefused, AssertionError)):
        Agent(client, token=issued["access_token"]).call("list_accounts")


def test_a_token_minted_on_the_command_line_still_works(client):
    """The regression that would be quietest.

    Turning authentication on hands the SDK the right to refuse anything it does not
    recognise, and every client configured the old way holds exactly such a token.
    """
    assert [a["name"] for a in Agent(client, token=TOKEN).call("list_accounts")] == ["test"]


def test_an_expired_access_token_is_refused(client, service):
    issued = log_in(client)

    async def _work(scope):
        row = await scope.scalar(
            sa.select(m.OAuthToken).where(m.OAuthToken.kind == m.OAuthTokenKind.access)
        )
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await scope.commit()

    scoped(service, _work)

    with pytest.raises((ToolRefused, AssertionError)):
        Agent(client, token=issued["access_token"]).call("list_accounts")


def test_a_stale_consent_request_cannot_be_answered(client, service):
    verifier, challenge = pkce()
    client_id = register(client)
    request_id = ask(client, client_id, challenge)

    async def _work(scope):
        row = await scope.scalar(
            sa.select(m.OAuthAuthorization).where(m.OAuthAuthorization.request_id == request_id)
        )
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await scope.commit()

    scoped(service, _work)

    assert client.get(f"/consent?request={request_id}").status_code == 404
    assert oauth.REQUEST_TTL > dt.timedelta(0)


# ------------------------------------------------------------- background tasks


def _stranded_accepted(client, *, with_failed_task: bool) -> tuple[int, int | None]:
    """An accepted bundle whose apply has not happened — with or without a failed task.

    Built directly rather than through the routes, so the live dispatcher (which only
    claims *queued* tasks) leaves the state alone while the page renders.
    """
    service = client.app.state.task_runner.service
    proposed = _propose(Agent(client))

    async def _work(scope):
        bundle = await scope.get(m.Bundle, proposed["bundle_id"])
        reviewer_row = await scope.scalar(
            sa.select(m.Producer).where(m.Producer.kind == m.ProducerKind.person)
        )
        if reviewer_row is None:
            reviewer_row = scope.add(m.Producer(kind=m.ProducerKind.person, name="reviewer"))
            await scope.flush()
        await suggest.accept(
            scope,
            bundle,
            reviewer_row,
            reviewed_through=suggest.shown_through(bundle),
        )
        task_id = None
        if with_failed_task:
            task = m.Task(
                kind=m.TaskKind.apply_bundle,
                status=m.TaskStatus.failed,
                account_id=bundle.account_id,
                subject_id=bundle.id,
                error="the mailbox went away mid-apply",
            )
            scope.add(task)
            await scope.flush()
            task_id = task.id
        await scope.commit()
        return proposed["bundle_id"], task_id

    return scoped(service, _work)


def test_an_accepted_bundle_is_visible_on_the_queue_not_invisible(client):
    """The stuck-state hole, closed at the page: accepted work has a section of its own."""
    bundle_id, task_id = _stranded_accepted(client, with_failed_task=True)
    page = client.get("/").text
    assert "Being applied" in page
    assert f"/bundle/{bundle_id}" in page
    assert "the mailbox went away mid-apply" in page, "the failure is shown, not hidden"
    assert f"/task/{task_id}/retry" in page, "and there is a way to try again"


def test_the_bundle_page_shows_the_apply_and_retry_finishes_it(client, backend):
    bundle_id, task_id = _stranded_accepted(client, with_failed_task=True)
    page = client.get(f"/bundle/{bundle_id}").text
    assert "being applied" in page.lower()
    assert "the mailbox went away mid-apply" in page

    response = as_a_person(client, f"/task/{task_id}/retry", follow_redirects=True)
    assert response.status_code == 200
    settle(client)
    assert len(backend.folders["Archive"].messages) == 1
    assert "applied" in client.get(f"/bundle/{bundle_id}").text


def test_a_failed_body_fetch_is_shown_next_to_the_button(client):
    """The silent contextlib.suppress hole, closed on the page."""
    service = client.app.state.task_runner.service
    proposed = _propose(Agent(client))

    async def _work(scope):
        bundle = await scope.get(m.Bundle, proposed["bundle_id"])
        suggestion = sorted(bundle.suggestions, key=lambda s: s.id)[0]
        task = m.Task(
            kind=m.TaskKind.fetch_body,
            status=m.TaskStatus.failed,
            account_id=bundle.account_id,
            subject_id=suggestion.message_id,
            payload={"bundle_id": bundle.id},
            error="the server closed the connection",
        )
        scope.add(task)
        await scope.commit()

    scoped(service, _work)
    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    assert "the server closed the connection" in page
    assert "retry" in page


def test_request_sync_and_task_status_carry_the_work_to_its_result(client):
    """The agent's shape of the same queue: enqueue, poll, read the report."""
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    asked = agent.call("request_sync", container_id=containers["INBOX"]["id"])
    assert asked["status"] in ("queued", "running")
    assert "task_id" in asked

    settle(client)
    outcome = agent.call("task_status", task_id=asked["task_id"])
    assert outcome["status"] == "done"
    assert outcome["result"]["folders"] == 1

    joined = agent.call("request_sync", container_id=containers["INBOX"]["id"])
    settle(client)
    assert joined["coalesced"] is False, "a finished sync does not satisfy a fresh ask"


def test_request_body_becomes_a_task_and_then_the_body(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    listed = agent.call("list_messages", container_id=containers["INBOX"]["id"], limit=1)
    message_id = listed["messages"][0]["message_id"]

    first = agent.call("request_body", message_id=message_id)
    assert "task_id" in first and first["status"] in ("queued", "running")
    settle(client)
    again = agent.call("request_body", message_id=message_id)
    assert again.get("body"), "once cached, the body is the answer"


async def test_progress_reaches_the_page_as_a_turbo_stream(service):
    """One event off the wire: the SSE endpoint speaks <turbo-stream>.

    Driven at the ASGI level: TestClient buffers whole responses, so an endless stream
    can never be read through it — here the raw response messages are read until the
    first event and the request is cancelled, which is also what a closing tab does.
    """
    app = create_app(service, session_key=SESSION_KEY)

    async with app.router.lifespan_context(app):
        # A running task the dispatcher will not touch (it only claims queued ones).
        async with service.scope() as scope:
            account = await scope.scalar(sa.select(m.Account))
            task = m.Task(
                kind=m.TaskKind.sync_account,
                status=m.TaskStatus.running,
                account_id=account.id,
                subject_id=account.id,
                progress_done=3,
                progress_total=9,
                progress_note="INBOX",
            )
            scope.add(task)
            await scope.commit()
            task_id = task.id

        messages: asyncio.Queue = asyncio.Queue()

        async def receive():  # noqa: ANN202
            await asyncio.sleep(3600)

        async def send(message):  # noqa: ANN001, ANN202
            await messages.put(message)

        scope_dict = {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"cookie", f"mailmind_session={SESSION_KEY}".encode()),
            ],
            "scheme": "http",
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 1234),
        }
        request = asyncio.ensure_future(app(scope_dict, receive, send))
        try:
            start = await asyncio.wait_for(messages.get(), timeout=10)
            assert start["type"] == "http.response.start" and start["status"] == 200
            body = b""
            while b"</turbo-stream>" not in body:
                chunk = await asyncio.wait_for(messages.get(), timeout=10)
                body += chunk.get("body", b"")
        finally:
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await request

        event = body.decode()
        assert event.startswith("data: <turbo-stream")
        assert f'target="task-{task_id}"' in event
        assert "3/9" in event and "INBOX" in event


def test_a_schema_problem_answers_every_surface_with_503_not_tracebacks(client):
    """What the drift check buys at the page: one honest answer everywhere."""
    service = client.app.state.task_runner.service
    service.schema_problem = "this database is at 0006folder and this build needs 0007task"
    try:
        page = client.get("/")
        assert page.status_code == 503
        assert "0007task" in page.text
        assert "mailmindctl migrate" in page.text
        assert client.post("/accounts/choose", data={"account_id": "1"}).status_code == 503
        assert client.get("/mcp/").status_code == 503, "machine paths say it too"
    finally:
        service.schema_problem = None
    assert client.get("/").status_code == 200, "and it is only about the schema"


def test_a_prompt_given_an_unsubstituted_placeholder_still_answers(client):
    """opencode sends the literal `$1` when its prompt-argument UI is skipped; MCP
    prompt arguments are strings anyway. An instruction comes back, not a traceback."""
    agent = Agent(client)
    answer = agent._post(
        "prompts/get",
        {"name": "triage_mailbox", "arguments": {"container_id": "$1"}},
    )
    assert "error" not in answer, answer
    text = answer["result"]["messages"][0]["content"]["text"]
    assert "list_containers" in text, "it says how to find the real id"
    assert "$1" not in text

    numbered = agent._post(
        "prompts/get",
        {"name": "triage_mailbox", "arguments": {"container_id": "3"}},
    )
    assert "container 3" in numbered["result"]["messages"][0]["content"]["text"]
