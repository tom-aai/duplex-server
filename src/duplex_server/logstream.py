"""Fan the worker's log lines out to browser log panes.

The worker logs the whole delegation story to the ``duplex.worker`` logger: the delegation
request, each ``-> commentary.append`` / ``-> thinking.append`` send, the tool calls, the
brain answer, and the reattach backoff. We attach one handler to that logger and push each
formatted line to every connected browser.

We keep no backlog. A new subscriber starts empty, so opening or reloading the page reads as
a fresh start and never replays a previous call's lines. A refresh tears down the WebRTC call
anyway, so there is no live call whose history a backlog could usefully restore.

The ``/logs`` endpoint in ``broker.py`` turns each line into a Server-Sent Event. The console
still gets the same lines, because the logger also propagates to the root handler.
"""

from __future__ import annotations

import asyncio
import logging

# Match the console's default format, so the pane reads like the terminal.
_FORMAT = logging.Formatter("%(levelname)s:%(name)s:%(message)s")


class LogStream(logging.Handler):
    """A logging handler that mirrors each record to every open SSE subscriber."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(_FORMAT)
        self._subscribers: set[asyncio.Queue[str]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        """Format one record and hand it to every subscriber.

        The worker logs from its own asyncio tasks, which run on the server's event loop.
        So this runs on that loop too, and ``put_nowait`` reaches the SSE generators with
        no cross-thread hop. A full queue means a slow reader; we drop the line for it
        rather than block the worker.
        """
        line = self.format(record)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue[str]:
        """Open one subscriber. It starts empty: no past lines are replayed."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Close one subscriber when its browser goes away."""
        self._subscribers.discard(queue)
