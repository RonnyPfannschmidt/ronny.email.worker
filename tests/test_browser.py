"""What only a browser can see: where the page is, what is open, what got replaced.

Not in the default environment, and needs Chrome:
``uv run --with playwright python -m pytest tests/test_browser.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from mailmind.web.next import script_for
from tests.targets.fake import FakeBackend

playwright = pytest.importorskip("playwright.async_api")
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
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service, session_key=KEY),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
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
        await browser.close()


async def _open_one_far_down(page, base, path):
    await page.goto(base + path)
    details = page.locator("details").nth(60)
    await details.wait_for()
    await details.scroll_into_view_if_needed()
    await details.locator("summary").click()
    await page.evaluate("window.mark = 1; document.body.dataset.mark = 1")
    return details, await page.evaluate("scrollY")


@pytest.mark.skipif(script_for("src/bundle.js") is None, reason="the interface is not built")
async def test_fetching_a_body_leaves_everything_else_where_it_was(page, bundle_at):
    base, bundle_id, _ = bundle_at
    details, was = await _open_one_far_down(page, base, f"/next/bundle/{bundle_id}")
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


@pytest.mark.skipif(script_for("src/bundle.js") is None, reason="the interface is not built")
async def test_excluding_and_then_accepting_moves_only_what_was_left(page, bundle_at):
    base, bundle_id, backend = bundle_at
    await page.goto(f"{base}/next/bundle/{bundle_id}")
    await page.get_by_role("button", name="exclude").first.click()
    await page.get_by_text("excluded").wait_for()
    assert "80 of 81" in await page.locator("h1").inner_text()

    await page.get_by_role("button", name="Accept").click()
    for _ in range(600):
        if len(backend.folders["Archive"].messages) == ITEMS - 1:
            break
        await asyncio.sleep(0.1)
    assert len(backend.folders["Archive"].messages) == ITEMS - 1
