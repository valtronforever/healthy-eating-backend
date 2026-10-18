from typing import Any

import httpx2
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport

from tests.oauth_helpers import MCP_RESOURCE


def mcp_client(app: FastAPI, access_token: str) -> Client[StreamableHttpTransport]:
    """An MCP client talking to the app in-process, authenticated with our access token."""

    def http_client_factory(**kwargs: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=StreamingASGITransport(app), **kwargs)

    transport = StreamableHttpTransport(
        url=MCP_RESOURCE,
        auth=access_token,
        # FastMCP passes more keyword arguments than the SDK's factory protocol declares.
        httpx_client_factory=http_client_factory,  # type: ignore[arg-type]
    )
    return Client(transport)
