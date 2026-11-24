"""A uvicorn server that can be stopped while somebody has a page open."""

from __future__ import annotations

import uvicorn


class Server(uvicorn.Server):
    """Ends the app's own event streams before waiting for connections to finish.

    uvicorn waits for every response, and the lifespan's shutdown comes after that, so an
    event stream left open in a tab would be waited for until the tab closed.  Its
    ``timeout_graceful_shutdown`` is not the answer: it cancels requests in the middle of
    a query, which SQLAlchemy's cleanup of the connection can hang on.
    """

    def __init__(self, config: uvicorn.Config, app) -> None:  # noqa: ANN001
        super().__init__(config)
        self._app = app

    async def shutdown(self, sockets=None) -> None:  # noqa: ANN001
        runner = getattr(self._app.state, "task_runner", None)
        if runner is not None:
            runner.hub.close()
        await super().shutdown(sockets)
