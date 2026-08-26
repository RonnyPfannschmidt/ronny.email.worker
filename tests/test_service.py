"""The runtime's own behaviour: how connections are handed out.

Not the mail, not the review — the part of :class:`~mailmind.service.Service` that both
surfaces share and that neither of them can see going wrong.
"""

from __future__ import annotations

import asyncio

from mailmind.config import AccountConfig, Config, Login
from mailmind.db import models as m
from mailmind.service import Service
from tests.targets.fake import FakeBackend


def _service(tmp_path) -> Service:
    return Service(
        Config(
            database_url=f"sqlite:///{tmp_path / 'mm.db'}",
            accounts=(
                AccountConfig(
                    name="test", host="h", login=Login(username="u", password="env://X")
                ),
            ),
        ),
        backend_factory=lambda _config: FakeBackend(),
    )


async def test_a_backend_is_held_by_one_task_at_a_time(tmp_path):
    """IMAP is stateful: a connection carries a selected folder.

    Two tasks sharing one do not fail — they read the wrong folder. Proved by giving a
    second task every chance to get in while the first is inside, the point being that
    it does not, and then that it does once the first lets go.
    """
    service = _service(tmp_path)
    account = m.Account(name="test", host="h", username="u", password_url="env://X")

    holding, second_is_in, release = (asyncio.Event() for _ in range(3))

    async def first() -> None:
        async with service.backend(account):
            holding.set()
            await release.wait()

    async def second() -> None:
        async with service.backend(account):
            second_is_in.set()

    task_one = asyncio.create_task(first())
    await asyncio.wait_for(holding.wait(), timeout=10)
    task_two = asyncio.create_task(second())

    # Every chance to get in: yield the loop repeatedly while the first still holds it.
    for _ in range(20):
        await asyncio.sleep(0)
    assert not second_is_in.is_set(), "two tasks held the same IMAP connection at once"

    release.set()
    await asyncio.wait_for(asyncio.gather(task_one, task_two), timeout=10)
    assert second_is_in.is_set(), "the connection was never handed on"


async def test_one_connection_per_account_however_many_ask_at_once(tmp_path):
    """Racing to open the same account must not log in twice and leak one of them."""
    service = _service(tmp_path)
    account = m.Account(name="test", host="h", username="u", password_url="env://X")

    seen: list[object] = []

    async def take() -> None:
        async with service.backend(account) as backend:
            seen.append(backend)

    await asyncio.wait_for(asyncio.gather(*(take() for _ in range(6))), timeout=10)

    assert len(seen) == 6
    assert len({id(backend) for backend in seen}) == 1

    service.close()
    assert seen[0].closed
