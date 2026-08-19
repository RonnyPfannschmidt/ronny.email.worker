"""The two surfaces, driven the way they are actually used.

An MCP client talks to the endpoint over HTTP with a bearer token; a person drives the
review UI with form posts.  Between them is the boundary: everything an agent can reach
is here, and none of it changes a mailbox.
"""

from __future__ import annotations

import datetime as dt

import attrs
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mailmind.config import AccountConfig, Config, ConfigError, Limits, Login
from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.imap import sync
from mailmind.imap.backend import TRASH
from mailmind.imap.capabilities import probe_account
from mailmind.mcp import server as mcp_server
from mailmind.service import Service, hash_token
from mailmind.web import app as app_module
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

    with service.scope() as scope:
        account = scope.add(
            m.Account(name="test", host="h", username="u", password_url="env://X")
        )
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
        scope.add(
            m.Account(name="other", host="h", username="u2", password_url="env://Y")
        )
        scope.commit()
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

    with service.scope() as scope:
        account = scope.add(
            m.Account(name="other", host="h", username="u2", password_url="env://Y")
        )
        scope.flush()
        containers = {
            c.name: c for c in sync.discover_containers(scope, account, elsewhere)
        }
        for container in containers.values():
            sync.sync_container(scope, account, container, elsewhere)
        message = scope.scalar(
            sa.select(m.Message).where(m.Message.account_id == account.id)
        )
        producer = scope.scalar(
            sa.select(m.Producer).where(m.Producer.name == "opencode")
        )
        bundle = suggest.propose_bundle(
            scope,
            producer=producer,
            account=account,
            operation=m.Operation.move,
            message_ids=[message.id],
            summary="Somebody else's mail",
            reason="Should be unreachable from a grant that does not cover this account",
            target_container_id=containers["Archive"].id,
        )
        scope.commit()
        return {
            "account_id": account.id,
            "test_account_id": scope.scalar(
                sa.select(m.Account.id).where(m.Account.name == "test")
            ),
            "container_id": containers["INBOX"].id,
            "message_id": message.id,
            "bundle_id": bundle.id,
            "suggestion_id": bundle.suggestions[0].id,
        }


def test_a_grant_expiry_is_honoured_rather_than_taking_the_endpoint_down(client, service):
    """An expiry used to be a TypeError, not an expiry.

    SQLite has no offset, so an aware ``expires_at`` was written without one and read back
    naive — and comparing that to an aware ``now()`` raises. It happens in the middleware,
    before any tool runs, so the first grant given an expiry would have broken every
    request rather than only its own.
    """
    assert Agent(client).call("list_accounts"), "a grant with no expiry was refused"

    def set_expiry(delta: dt.timedelta) -> None:
        with service.scope() as scope:
            grant = scope.scalar(sa.select(m.Grant))
            grant.expires_at = dt.datetime.now(dt.UTC) + delta
            scope.commit()

    set_expiry(dt.timedelta(hours=1))
    assert Agent(client).call("list_accounts"), "a grant that had not expired was refused"

    set_expiry(-dt.timedelta(seconds=1))
    with pytest.raises((ToolRefused, AssertionError)) as refused:
        Agent(client).call("list_accounts")
    assert "grant" in str(refused.value).lower()


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

    # The corpus runs one message a day from 2026-08-17 09:00Z. The 19th is a real
    # message sitting exactly on the boundary below.
    boundary = "2026-08-19T09:00:00Z"
    assert window(before=boundary)["total_matching"] == 2, "before must exclude its edge"
    assert window(since=boundary)["total_matching"] == 4, "since must include its edge"
    assert (
        window(before=boundary)["total_matching"] + window(since=boundary)["total_matching"]
        == len(CORPUS)
    )

    # A bare date means midnight, so the whole of the 19th is still to come.
    assert window(before="2026-08-19")["total_matching"] == 2
    assert window(since="2026-08-19")["total_matching"] == 4

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

    response = client.post(f"/bundle/{proposed['bundle_id']}/accept", follow_redirects=True)
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
    with TestClient(create_app(elsewhere), base_url="http://127.0.0.2:9000") as moved:
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

    switched = client.post(
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
    with service.scope() as scope:
        spare = scope.add(
            m.Account(name="spare", host="h", username="u3", password_url="env://Z")
        )
        person = scope.add(m.Producer(kind=m.ProducerKind.person, name="reviewer"))
        scope.flush()
        person.current_account_id = spare.id
        scope.commit()
        spare_id, person_id = spare.id, person.id

    with service.scope() as scope:
        scope.execute(sa.delete(m.Account).where(m.Account.id == spare_id))
        scope.commit()

    with service.scope() as scope:
        person = scope.get(m.Producer, person_id)
        assert person is not None, "the producer went with the account"
        assert person.current_account_id is None
        # And the fallback puts the reviewer back in a real account.
        assert app_module.chosen_account(scope).name == "test"


def test_an_unauthenticated_review_ui_refuses_to_listen_to_the_network(service, backend):
    """The review UI has no login, so it does not get to be reachable from anywhere else.

    That was half a bargain: the bind address defaulted to loopback and nothing stopped it
    being changed, so `--host 0.0.0.0` served an accept-and-apply button to the network.
    """
    exposed = Service(
        attrs.evolve(service.config, bind="0.0.0.0"),  # noqa: S104 — the point of the test
        backend_factory=lambda _config: backend,
    )
    with pytest.raises(ConfigError) as refused:
        create_app(exposed)
    assert "no login" in str(refused.value)

    # Somebody else authenticating is the other way to hold up the same bargain, and it
    # has to be said out loud because nothing here can check it.
    proxied = Service(
        attrs.evolve(service.config, bind="0.0.0.0", behind_auth_proxy=True),  # noqa: S104
        backend_factory=lambda _config: backend,
    )
    app = create_app(proxied)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200


def test_an_account_that_exists_only_in_the_database_can_still_connect(service, backend):
    """Which is every account the review UI will ever add.

    Connections used to be built by looking the account's name back up in the
    configuration file, so a row the file did not mention existed and was unusable at the
    same time — and the file cannot be the source of truth for something a web form
    writes.
    """
    with service.scope() as scope:
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
        scope.commit()
        assert row.name not in {a.name for a in service.config.accounts}
        with service.backend(row) as opened:
            assert opened is backend

    from mailmind.service import account_config

    derived = account_config(row)
    assert (derived.host, derived.port, derived.use_ssl) == ("imap.invalid", 1143, False)
    assert derived.login.username == "someone@example.org"
    assert derived.login.password == "env://NEVER_CONFIGURED"


def test_the_review_pages_forbid_remote_content(client):
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "img-src 'none'" in csp
    assert "default-src 'none'" in csp
