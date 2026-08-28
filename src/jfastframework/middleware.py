"""Edge protections the kernel applies before a request reaches your code.

Caddy or an ingress covers some of this when one is in front. Not every
deployment has one: ``jfast deploy function`` puts a service on Lambda or Cloud
Run with nothing between it and the internet, and a developer running
``uvicorn`` locally has nothing either. A framework that only behaves when
something else is correctly configured is a framework with a footgun.

Both middlewares here are plain ASGI rather than ``BaseHTTPMiddleware``. That
is deliberate: ``BaseHTTPMiddleware`` buffers the response through an anyio
stream, which breaks streaming responses and makes a timeout land in the wrong
place.

Everything is off unless configured. A body limit or a request timeout is a
policy decision with a wrong answer for somebody, so the kernel refuses to
guess one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from jfastframework.errors import PROBLEM_CONTENT_TYPE

Scope = dict[str, Any]
Receive = Any
Send = Any


def _problem(status: int, title: str, detail: str) -> tuple[dict[str, Any], bytes]:
    body = json.dumps(
        {"type": "about:blank", "title": title, "status": status, "detail": detail}
    ).encode()
    start = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", PROBLEM_CONTENT_TYPE.encode()),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    return start, body


async def _send_problem(send: Send, status: int, title: str, detail: str) -> None:
    start, body = _problem(status, title, detail)
    await send(start)
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Reject a request body larger than ``max_bytes`` with 413.

    Checks ``Content-Length`` first, which rejects the common case before a
    single byte is read. A chunked upload has no length to check, so the bytes
    are counted as they arrive and the request is refused the moment it goes
    over -- not after the whole thing has been buffered into memory, which is
    the outcome this exists to prevent.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    await _send_problem(
                        send,
                        413,
                        "Payload Too Large",
                        f"Request body of {declared} bytes exceeds the "
                        f"{self.max_bytes} byte limit.",
                    )
                    return
                break

        seen = 0
        refused = False

        async def counting_receive() -> dict[str, Any]:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    refused = True
                    # Tell the application the body ended. It will see a short
                    # or empty body; the response it produces is discarded.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message  # type: ignore[no-any-return]

        started = False

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal started
            if refused:
                # The limit was hit mid-stream. Send 413 instead of whatever
                # the application decided from a truncated body.
                if not started:
                    started = True
                    await _send_problem(
                        send,
                        413,
                        "Payload Too Large",
                        f"Request body exceeds the {self.max_bytes} byte limit.",
                    )
                return
            started = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    def __repr__(self) -> str:
        return f"<BodySizeLimitMiddleware max_bytes={self.max_bytes}>"


class RequestTimeoutMiddleware:
    """Fail a request that outlives ``seconds`` with 504.

    Once the response has started streaming there is nothing honest left to do
    -- the status line is already on the wire -- so the timeout only applies
    before the first byte is sent. That is the window where a stuck dependency
    holds a connection open forever.
    """

    def __init__(self, app: Any, *, seconds: float) -> None:
        self.app = app
        self.seconds = seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(self.seconds):
                await self.app(scope, receive, tracking_send)
        except TimeoutError:
            if started:
                raise
            await _send_problem(
                send,
                504,
                "Gateway Timeout",
                f"The request did not complete within {self.seconds}s.",
            )

    def __repr__(self) -> str:
        return f"<RequestTimeoutMiddleware seconds={self.seconds}>"
