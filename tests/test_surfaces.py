"""The two surfaces, driven the way they are actually used.

An MCP client talks to the endpoint over HTTP with a bearer token; a person drives the
review UI with form posts.  Between them is the boundary: everything an agent can reach
is here, and none of it changes a mailbox.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mailmind.config import AccountConfig, Config, Limits
from mailmind.db import models as m
from mailmind.db.scope import unscoped_session
from mailmind.imap import sync
from mailmind.imap.capabilities import probe_account
from mailmind.mcp import server as mcp_server
from mailmind.service import Service, hash_token
from mailmind.web.app import create_app
from tests.corpus import CORPUS
from tests.targets.fake import FakeBackend

TOKEN = "test-token-for-opencode"


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
    service = Service(
        Config(
            database_url=url,
            limits=Limits(max_messages_per_request=3),
            accounts=(AccountConfig(name="test", host="h", username="u", secret_ref="env:X"),),
        ),
        backend_factory=lambda _config: backend,
    )
    m.Base.metadata.create_all(service.engine)
    with service.engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE VIRTUAL TABLE message_fts USING fts5(subject, from_text, "
                "preview, message_id UNINDEXED, tenant_id UNINDEXED, account_id UNINDEXED)"
            )
        )
    with unscoped_session(service.sessions) as session:
        session.add(m.Tenant(id=0, name="tenant-zero"))
        session.commit()

    with service.scope() as scope:
        account = scope.add(m.Account(name="test", host="h", username="u", secret_ref="env:X"))
        scope.flush()
        for cap in ("CONDSTORE", "MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE"):
            scope.add(m.AccountCapability(account_id=account.id, name=cap))
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        scope.flush()
        grant = scope.add(
            m.Grant(
                producer_id=producer.id,
                token_hash=hash_token(TOKEN),
                capabilities=["observe", "suggest", "assess"],
            )
        )
        scope.flush()
        scope.add(m.GrantAccount(grant_id=grant.id, account_id=account.id))
        probe_account(scope, account, backend)
        for container in sync.discover_containers(scope, account, backend):
            sync.sync_container(scope, account, container, backend)
        scope.commit()
    return service


@pytest.fixture
def client(service):
    # A host the MCP endpoint's rebinding protection accepts.
    with TestClient(create_app(service), base_url="http://127.0.0.1:8765") as client:
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

    def read(self, uri: str):  # noqa: ANN201
        import json

        result = self._post("resources/read", {"uri": uri})
        if "error" in result:
            raise AssertionError(result["error"])
        return json.loads(result["result"]["contents"][0]["text"])


class ToolRefused(Exception):
    pass


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
    "propose_bundle",
    "add_assessment",
    "withdraw_bundle",
}


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


def test_without_a_token_there_is_no_view_at_all(client):
    agent = Agent(client, token=None)
    try:
        agent.call("list_accounts")
    except (ToolRefused, AssertionError) as exc:
        assert "grant" in str(exc).lower()
    else:
        raise AssertionError("an unauthenticated caller got a view")


def test_an_agent_sees_only_the_accounts_its_grant_covers(client, service):
    with service.scope() as scope:
        scope.add(m.Account(name="other", host="h", username="u2", secret_ref="env:Y"))
        scope.commit()
    accounts = Agent(client).call("list_accounts")
    assert [a["name"] for a in accounts] == ["test"]


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


def test_summarising_senders_is_one_call_not_an_enumeration(client):
    agent = Agent(client)
    account = agent.call("list_accounts")[0]
    inbox = next(
        c
        for c in agent.call("list_containers", account_id=account["id"])
        if c["name"] == "INBOX"
    )
    senders = agent.call("summarize_senders", container_id=inbox["id"])
    assert len(senders) == len(CORPUS)
    assert all(s["count"] >= 1 for s in senders)
    lists = agent.call("summarize_lists", container_id=inbox["id"])
    assert [entry["list_id"] for entry in lists] == ["Weekly <weekly.list.example>"]
    assert lists[0]["has_unsubscribe"] is True


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


def _propose(agent: Agent) -> dict:
    account = agent.call("list_accounts")[0]
    containers = {c["name"]: c for c in agent.call("list_containers", account_id=account["id"])}
    senders = agent.call("summarize_senders", container_id=containers["INBOX"]["id"])
    newsletter = next(s for s in senders if s["from_address"] == "news@list.example")
    messages = agent.call(
        "list_messages",
        container_id=containers["INBOX"]["id"],
        from_address=newsletter["from_address"],
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


def test_accepting_in_the_ui_actually_moves_the_mail(client, backend):
    agent = Agent(client)
    proposed = _propose(agent)
    assert len(backend.folders["Archive"].messages) == 0

    response = client.post(f"/bundle/{proposed['bundle_id']}/accept", follow_redirects=True)
    assert response.status_code == 200
    assert len(backend.folders["Archive"].messages) == 1
    assert "applied" in response.text


def test_the_ui_refuses_to_accept_around_something_that_moved(client, backend, service):
    agent = Agent(client)
    proposed = _propose(agent)

    # The person filed it themselves, in their own mail client.
    uid = next(iter(backend.folders["INBOX"].messages))
    backend.out_of_band_move("INBOX", uid, "Archive")
    with service.scope() as scope:
        account = scope.scalar(sa.select(m.Account).where(m.Account.name == "test"))
        inbox = scope.scalar(sa.select(m.Container).where(m.Container.name == "INBOX"))
        sync.sync_container(scope, account, inbox, backend)
        scope.commit()

    page = client.get(f"/bundle/{proposed['bundle_id']}").text
    if "moved since this was proposed" in page:
        assert "acknowledge_stale" in page
    response = client.post(f"/bundle/{proposed['bundle_id']}/accept", follow_redirects=True)
    assert response.status_code == 200


def test_a_rejection_can_carry_a_reason_and_is_as_easy_as_accepting(client, backend):
    agent = Agent(client)
    proposed = _propose(agent)
    response = client.post(
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


def test_the_review_pages_forbid_remote_content(client):
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "img-src 'none'" in csp
    assert "default-src 'none'" in csp
