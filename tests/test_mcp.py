import httpx
from fastapi import FastAPI

from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, register_client, sign_in


async def test_mcp_endpoint_requires_token(http: httpx.AsyncClient) -> None:
    response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


async def test_get_profile_tool_returns_signed_in_user(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    client = await register_client(http, name="Claude")
    tokens = await sign_in(http, client, email="grace@example.com")
    me = (await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))).json()

    async with mcp_client(app, tokens["access_token"]) as mcp:
        tools = {tool.name for tool in await mcp.list_tools()}
        assert {"get_profile", "log_meal"} <= tools
        result = await mcp.call_tool("get_profile", {})

    assert result.structured_content is not None
    assert result.structured_content["user_id"] == me["id"]
    assert result.structured_content["linked_logins"] == ["dev"]
