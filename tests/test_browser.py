"""What only a browser can see: where the page is, what is open, what got replaced.

Not in the default environment, and needs Chrome:
``uv run --with playwright python -m pytest tests/test_browser.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time

import pytest
import sqlalchemy as sa
import uvicorn

from mailmind.config import Config, Limits
from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.imap import sync
from mailmind.imap.capabilities import probe_account
from mailmind.service import Service
from mailmind.suggest import model as suggest
from mailmind.web.app import SESSION_KEY_PARAM, create_app
from mailmind.web.bundle_page import script_for
from mailmind.web.serving import Server
from tests.targets.fake import FakeBackend

playwright = pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.skipif(
    script_for("src/bundle.js") is None, reason="the interface is not built"
)
KEY = "browser-test-key"
ITEMS = 81
HOSTILE = "<img src=x onerror=document.title='owned'>"


class Slow(FakeBackend):
    """Slow where a server is: inside the call, which runs off the event loop."""

    slow = False

    def fetch_raw(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        if self.slow:
            time.sleep(0.4)
        return super().fetch_raw(*args, **kwargs)


@pytest.fixture
async def bundle_at(tmp_path):
    backend = Slow()
    for name, use in (("INBOX", None), ("Archive", "archive"), ("Trash", "trash")):
        backend.add_folder(name, special_use=use)
    for n in range(ITEMS):
        backend.add_message(
            "INBOX",
            f"From: a{n}@example.org\r\nTo: me@example.org\r\nSubject: {HOSTILE} {n}\r\n"
            f"Date: Wed, 19 Aug 2026 09:00:00 +0000\r\nMessage-ID: <{n}@example.org>\r\n"
            f"\r\nbody number {n} {HOSTILE}\r\n".encode(),
        )
    url = f"sqlite:///{tmp_path / 'mm.db'}"
    upgrade_to_head(url)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    service = Service(
        Config(database_url=url, port=port, limits=Limits(max_messages_per_request=100)),
        backend_factory=lambda _config: backend,
    )
    async with service.scope() as scope:
        account = scope.add(
            m.Account(name="test", host="h", username="u", password_url="env://X")
        )
        producer = scope.add(m.Producer(kind=m.ProducerKind.agent, name="opencode"))
        await scope.flush()
        await probe_account(scope, account, backend)
        containers = {
            c.name: c for c in await sync.discover_containers(scope, account, backend)
        }
        await sync.sync_container(scope, account, containers["INBOX"], backend)
        bundle = await suggest.propose_bundle(
            scope,
            producer=producer,
            account=account,
            operation=m.Operation.move,
            message_ids=await scope.all(sa.select(m.Message.id).order_by(m.Message.id)),
            target_container_id=containers["Archive"].id,
            summary="s",
            reason="r",
        )
        await scope.commit()
        bundle_id = bundle.id
    backend.slow = True
    app = create_app(service, session_key=KEY)
    server = Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"), app)
    server.install_signal_handlers = lambda: None
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}", bundle_id, backend
    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        await serving
    await service.dispose()


@pytest.fixture
async def page(bundle_at):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        page = await browser.new_page(viewport={"width": 1100, "height": 700})
        page.problems = []
        page.on(
            "console",
            lambda msg: page.problems.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: page.problems.append(str(exc)))
        await page.goto(f"{bundle_at[0]}/?{SESSION_KEY_PARAM}={KEY}")
        yield page
        await page.unroute_all(behavior="ignoreErrors")
        await browser.close()


async def _open_one_far_down(page, base, path):
    await page.goto(base + path)
    details = page.locator("details").nth(60)
    await details.wait_for()
    await details.scroll_into_view_if_needed()
    await details.locator("summary").click()
    await page.evaluate("window.mark = 1; document.body.dataset.mark = 1")
    return details, await page.evaluate("scrollY")


async def test_fetching_a_body_leaves_everything_else_where_it_was(page, bundle_at):
    base, bundle_id, _ = bundle_at
    details, was = await _open_one_far_down(page, base, f"/bundle/{bundle_id}")
    other = page.locator("details").nth(61)
    await other.locator("summary").click()

    await details.get_by_role("button").click()
    await details.locator(".body").wait_for(timeout=10_000)

    if shot := os.environ.get("MAILMIND_SCREENSHOT"):
        await page.screenshot(path=shot)
    assert "body number" in await details.locator(".body").inner_text()
    assert await other.get_attribute("open") is not None, "the other section closed"
    assert await page.evaluate("document.body.dataset.mark") == "1", "the page was replaced"
    assert abs(await page.evaluate("scrollY") - was) < 150
    assert page.problems == [], "something broke the script policy"
    assert await page.title() != "owned"
    assert await page.locator("img").count() == 0, "a stranger's markup became markup"


async def test_excluding_and_then_accepting_moves_only_what_was_left(page, bundle_at):
    base, bundle_id, backend = bundle_at
    await page.goto(f"{base}/bundle/{bundle_id}")
    await page.get_by_role("button", name="exclude").first.click()
    await page.get_by_text("excluded").wait_for()

    await page.get_by_role("button", name="Accept").click()
    for _ in range(600):
        if len(backend.folders["Archive"].messages) == ITEMS - 1:
            break
        await asyncio.sleep(0.1)
    assert len(backend.folders["Archive"].messages) == ITEMS - 1


async def test_a_bundle_that_grows_is_not_slipped_under_the_reader(page, bundle_at):
    """A laptop wakes, the stream reconnects, the page reads the bundle again."""
    base, bundle_id, _ = bundle_at

    async def ending(route):  # noqa: ANN001, ANN202
        await route.fulfill(
            body="retry: 500\nevent: ready\ndata: {}\n\n", content_type="text/event-stream"
        )

    await page.route("**/api/events", ending)
    await page.goto(f"{base}/bundle/{bundle_id}")
    await page.get_by_text("Accept — do this").wait_for()
    answer = await (await page.request.get(f"{base}/api/bundle/{bundle_id}")).json()
    read = answer["reviewed_through"]
    extra = {**answer["items"][0], "suggestion_id": read + 1, "subject": "slipped in"}
    grown = {**answer, "reviewed_through": read + 1, "items": [*answer["items"], extra]}

    async def serve(route):  # noqa: ANN001, ANN202
        await route.fulfill(json=grown)

    await page.route(f"**/api/bundle/{bundle_id}", serve)
    await page.get_by_text("arrived in this bundle after this page was drawn").wait_for(
        timeout=15_000
    )
    assert await page.get_by_text("slipped in").count() == 0
    assert await page.locator("[name=reviewed_through]").input_value() == str(read)


MESSAGE = {
    "suggestion_id": 7,
    "message_id": 3,
    "from_address": "a@example.org",
    "from_display_name": "A",
    "subject": "hello",
    "date": "2026-08-19T09:00:00",
    "currently_in": "INBOX",
    "would_move_to": "Archive",
    "status": "proposed",
    "stale_detail": None,
    "arrived_from": None,
    "arrived_late": False,
    "findings": [],
    "body": None,
    "task": None,
}
BUNDLE = {
    "bundle_id": 1,
    "status": "proposed",
    "operation": "move",
    "flag": None,
    "target_container": "Archive",
    "target_is_new": False,
    "producer": "opencode",
    "summary": "s",
    "reason": "r",
    "created_at": "2026-08-19T09:00:00",
    "expires_at": "2026-08-26T09:00:00",
    "decided_at": None,
    "decided_by": None,
    "decision_reason": None,
    "reviewed_through": 7,
    "queries": [],
    "self_assessed_by": [],
    "attempts": [],
    "apply_task": None,
    "items": [MESSAGE],
}
FOLDER = {
    "suggestion_id": 7,
    "message_id": None,
    "container_id": 4,
    "container": "Old",
    "cached_messages": 0,
    "children": ["Old/Sub"],
    "exists_on_server": True,
    "status": "proposed",
    "stale_detail": None,
    "findings": [],
    "body": None,
    "task": None,
}
FAILED = {
    "id": 5,
    "kind": "apply_bundle",
    "status": "failed",
    "subject_id": 1,
    "done": 0,
    "total": None,
    "note": None,
    "error": "went away mid-apply",
    "result": {},
}
STALE = {**MESSAGE, "status": "stale", "stale_detail": "the message has left INBOX"}
ACCEPT = "Accept — do this to the mailbox"

#: What differs from an ordinary bundle, what the page must say, and what it must not.
SAYS = {
    "ordinary": ({}, ["move → Archive", ACCEPT, "expires 26 Aug 2026"], ["does not exist yet"]),
    "new folder": ({"target_is_new": True}, ["does not exist yet", "will be created"], []),
    "discard": (
        {"operation": "discard_container", "target_container": None, "items": [FOLDER]},
        ["cannot lose mail", "Old/Sub", "nothing", "1 folders"],
        [],
    ),
    "found by search": (
        {"queries": [{"text": "news", "items": [7, 7]}]},
        ["found by searching for news", "the search is not run again"],
        [],
    ),
    "grew": (
        {
            "queries": [
                {"text": "more", "grew": True, "added": 1, "at": "2026-08-20T09:00:00"}
            ],
            "items": [{**MESSAGE, "arrived_late": True}],
        },
        ["added to since it was proposed", "20 Aug 2026", "added later"],
        ["found by searching"],
    ),
    "self assessed": ({"self_assessed_by": ["opencode"]}, ["the same producer that"], []),
    "delete": (
        {
            "operation": "delete",
            "target_container": None,
            "items": [{**MESSAGE, "would_move_to": None}],
        },
        ["Trash"],
        ["Archive"],
    ),
    "flag": (
        {"operation": "add_flag", "flag": "\\Seen", "target_container": None},
        ["(add_flag \\Seen)"],
        [],
    ),
    "partly stale": (
        {"items": [STALE, {**MESSAGE, "suggestion_id": 8, "message_id": 4}]},
        ["1 of these changed", "has left INBOX", "I have read what changed"],
        [],
    ),
    "closed itself": (
        {"status": "stale", "items": [STALE]},
        ["this closed itself"],
        [ACCEPT, "exclude"],
    ),
    "accepted and stuck": (
        {"status": "accepted", "apply_task": FAILED},
        ["being applied", "went away mid-apply", "retry"],
        [ACCEPT],
    ),
    "rejected": (
        {
            "status": "rejected",
            "decided_by": "ronny",
            "decision_reason": "keep these",
            "decided_at": "2026-08-20T09:00:00",
        },
        ["rejected by ronny", "keep these"],
        [ACCEPT],
    ),
    "applied": (
        {
            "status": "applied",
            "attempts": [
                {
                    "suggestion_id": 7,
                    "outcome": "applied",
                    "precondition": "conditional",
                    "guarantee_obtained": "best_effort",
                    "detail": None,
                }
            ],
        },
        ["What happened", "asked for conditional"],
        [ACCEPT],
    ),
    "fetch failed": (
        {"items": [{**MESSAGE, "task": {**FAILED, "kind": "fetch_body", "subject_id": 3}}]},
        ["went away mid-apply", "try again"],
        [],
    ),
}


@pytest.mark.parametrize("what", list(SAYS))
async def test_the_bundle_page_says(page, bundle_at, what):
    base, bundle_id, _ = bundle_at
    differs, says, says_not = SAYS[what]

    async def serve(route):  # noqa: ANN001, ANN202
        await route.fulfill(
            body=json.dumps({**BUNDLE, **differs}), content_type="application/json"
        )

    await page.route(f"**/api/bundle/{bundle_id}", serve)
    await page.goto(f"{base}/bundle/{bundle_id}")
    await page.get_by_text("What the producer says").wait_for()
    await page.evaluate("document.querySelectorAll('details').forEach((d) => (d.open = true))")
    text = " ".join((await page.locator("body").inner_text()).split())
    for words in says:
        assert words in text, text
    for words in says_not:
        assert words not in text, text
    assert page.problems == []


async def test_fetching_a_message_text_keeps_the_filing_form_as_it_was(page, bundle_at):
    """The old page reloaded, and took the destination somebody had typed with it."""
    base, _, _ = bundle_at
    await page.goto(f"{base}/mail/message/5")
    await page.locator("[name=target_container_name]").fill("Archive/half-typed")
    await page.evaluate("document.body.dataset.mark = 1")
    await page.get_by_role("button", name="fetch the text").click()
    await page.get_by_text("fetching").wait_for()
    await page.locator(".body").wait_for(timeout=10_000)

    assert "body number" in await page.locator(".body").inner_text()
    assert await page.locator("[name=target_container_name]").input_value() == (
        "Archive/half-typed"
    )
    assert await page.evaluate("document.body.dataset.mark") == "1", "the page was replaced"
    assert page.problems == []
    assert await page.locator("img").count() == 0, "a stranger's markup became markup"


async def test_a_message_text_that_could_not_be_fetched_says_so_and_offers_again(
    page, bundle_at
):
    base, _, backend = bundle_at
    backend.force_unreachable()
    await page.goto(f"{base}/mail/message/5")
    await page.get_by_role("button", name="fetch the text").click()
    await page.get_by_role("button", name="try again").wait_for(timeout=10_000)
    assert await page.locator(".banner.error").count() == 1

    backend.reachable = True
    await page.get_by_role("button", name="try again").click()
    await page.locator(".body").wait_for(timeout=10_000)


async def test_a_message_reached_from_the_folder_list_loads_its_script(page, bundle_at):
    """The list is driven by Turbo; the message page is not."""
    base, _, _ = bundle_at
    await page.goto(f"{base}/mail")
    await page.get_by_role("link", name="INBOX", exact=True).click()
    await page.locator("a[href^='/mail/message/']").first.click()
    await page.get_by_role("button", name="fetch the text").wait_for(timeout=10_000)
    await page.go_back()
    await page.locator("a[href^='/mail/message/']").first.wait_for()
