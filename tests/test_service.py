"""The runtime's own behaviour: how connections are handed out.

Not the mail, not the review — the part of :class:`~mailmind.service.Service` that both
surfaces share and that neither of them can see going wrong.
"""

from __future__ import annotations

import threading

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


def test_a_backend_is_held_by_one_worker_at_a_time(tmp_path):
    """IMAP is stateful: a connection carries a selected folder.

    Two threads sharing one do not fail — they read the wrong folder. Proved by giving a
    second worker every chance to get in while the first is inside, the point being that
    it does not, and then that it does once the first lets go.
    """
    service = _service(tmp_path)
    account = m.Account(name="test", host="h", username="u", password_url="env://X")

    holding, second_is_in, release = (threading.Event() for _ in range(3))

    def first() -> None:
        with service.backend(account):
            holding.set()
            release.wait(timeout=10)

    def second() -> None:
        with service.backend(account):
            second_is_in.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    assert holding.wait(timeout=10), "the first worker never got the connection"
    threads[1].start()

    assert not second_is_in.wait(timeout=0.25), (
        "two workers held the same IMAP connection at once"
    )
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert second_is_in.is_set(), "the connection was never handed on"


def test_one_connection_per_account_however_many_ask_at_once(tmp_path):
    """Racing to open the same account must not log in twice and leak one of them."""
    service = _service(tmp_path)
    account = m.Account(name="test", host="h", username="u", password_url="env://X")

    seen: list[object] = []
    start = threading.Barrier(6)

    def take() -> None:
        start.wait(timeout=10)
        with service.backend(account) as backend:
            seen.append(backend)

    threads = [threading.Thread(target=take) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(seen) == 6
    assert len({id(backend) for backend in seen}) == 1

    service.close()
    assert seen[0].closed
