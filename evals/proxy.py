"""Recording proxy in front of the HealthyEating MCP endpoint.

opencode talks to this proxy instead of the app. The proxy forwards every request and
records each `tools/call` with its result, so scenarios can check exactly what the model
sent. It can also make tool calls fail, to simulate an unreachable HealthyEating server.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

REQUEST_HEADERS = {
    "accept",
    "authorization",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
}
RESPONSE_HEADERS = {
    "cache-control",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
    "www-authenticate",
}


@dataclass
class ToolCall:
    """One `tools/call` request as seen on the wire, with its result."""

    name: str
    arguments: dict[str, Any]
    started_at: float
    structured_result: dict[str, Any] | None = None
    is_error: bool = False
    error_text: str | None = None
    failed_by_proxy: bool = False


class McpProxy:
    def __init__(self, upstream_url: str) -> None:
        self.upstream_url = upstream_url
        self.calls: list[ToolCall] = []
        self.fail_tool_calls = False
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None))
        self._app = Starlette(
            routes=[Route("/mcp", self._handle, methods=["GET", "POST", "DELETE"])]
        )
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def reset(self) -> None:
        self.calls.clear()
        self.fail_tool_calls = False

    def tool_calls(self, name: str) -> list[ToolCall]:
        return [call for call in self.calls if call.name == name]

    async def start(self, port: int) -> None:
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._server is not None and self._task is not None:
            self._server.should_exit = True
            await self._task
        await self._client.aclose()

    async def _handle(self, request: Request) -> Response:
        body = await request.body()
        call = _parse_tool_call(body) if request.method == "POST" else None
        if call is not None:
            self.calls.append(call)
            if self.fail_tool_calls:
                call.failed_by_proxy = True
                call.is_error = True
                call.error_text = "503 Service Unavailable (simulated by the eval proxy)"
                return Response("Service Unavailable", status_code=503)

        headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        upstream_request = self._client.build_request(
            request.method, self.upstream_url, headers=headers, content=body or None
        )
        upstream = await self._client.send(upstream_request, stream=True)
        response_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS
        }
        return StreamingResponse(
            _relay(upstream, call), status_code=upstream.status_code, headers=response_headers
        )


async def _relay(upstream: httpx.Response, call: ToolCall | None) -> AsyncIterator[bytes]:
    buffer = bytearray()
    try:
        async for chunk in upstream.aiter_bytes():
            if call is not None:
                buffer.extend(chunk)
            yield chunk
    finally:
        await upstream.aclose()
        if call is not None:
            _record_result(call, bytes(buffer), upstream.headers.get("content-type", ""))


def _parse_tool_call(body: bytes) -> ToolCall | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if isinstance(message, dict) and message.get("method") == "tools/call":
            params = message.get("params") or {}
            return ToolCall(
                name=str(params.get("name")),
                arguments=dict(params.get("arguments") or {}),
                started_at=time.time(),
            )
    return None


def _record_result(call: ToolCall, body: bytes, content_type: str) -> None:
    messages: list[Any] = []
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    messages.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    else:
        try:
            messages.append(json.loads(text))
        except ValueError:
            call.is_error = True
            call.error_text = text[:500]
            return

    for message in messages:
        if not isinstance(message, dict):
            continue
        if "error" in message:
            call.is_error = True
            call.error_text = str(message["error"].get("message", message["error"]))
        elif "result" in message:
            result = message["result"]
            call.is_error = bool(result.get("isError"))
            call.structured_result = result.get("structuredContent")
            if call.is_error:
                call.error_text = " ".join(
                    part.get("text", "") for part in result.get("content", [])
                )
