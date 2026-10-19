"""`offline_access`: advertised and accepted, so clients that only refresh when it is listed
(ChatGPT) keep their connection. Every sign-in still carries `nutrition`."""

from typing import Any

import httpx
import pytest

from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import (
    MCP_RESOURCE,
    REDIRECT_URI,
    Client,
    bearer,
    dev_login,
    exchange_code,
    pkce_pair,
    refresh,
)


async def test_discovery_lists_offline_access(http: httpx.AsyncClient) -> None:
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["scopes_supported"] == ["nutrition", "offline_access"]
    # Listed for the resource too: clients may take the scopes to request from here.
    resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["scopes_supported"] == ["nutrition", "offline_access"]


async def register(http: httpx.AsyncClient, scope: str | None) -> Client:
    body: dict[str, Any] = {
        "client_name": "ChatGPT",
        "redirect_uris": [REDIRECT_URI],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if scope is not None:
        body["scope"] = scope
    response = await http.post("/register", json=body)
    assert response.status_code == 201, response.text
    return Client(client_id=response.json()["client_id"], redirect_uri=REDIRECT_URI)


async def sign_in_with_scope(http: httpx.AsyncClient, client: Client, scope: str) -> dict[str, Any]:
    verifier, challenge = pkce_pair()
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
            "scope": scope,
            "resource": MCP_RESOURCE,
        },
    )
    assert response.status_code == 302, response.text
    assert (await http.get(response.headers["location"])).status_code == 200
    params = await dev_login(http, "chatgpt-user@example.com")
    tokens = await exchange_code(http, client, params["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    result: dict[str, Any] = tokens.json()
    return result


@pytest.mark.parametrize("registered_scope", [None, "nutrition offline_access"])
@pytest.mark.parametrize("requested", ["nutrition offline_access", "offline_access"])
async def test_offline_access_sign_in_keeps_working_after_refresh(
    app: Any, http: httpx.AsyncClient, registered_scope: str | None, requested: str
) -> None:
    client = await register(http, registered_scope)
    tokens = await sign_in_with_scope(http, client, requested)

    assert set(tokens["scope"].split()) == {"nutrition", "offline_access"}
    assert tokens["refresh_token"]
    me = await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 200

    refreshed = await refresh(http, client, tokens["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    access = refreshed.json()["access_token"]
    async with mcp_client(app, access) as mcp:
        tools = {tool.name for tool in await mcp.list_tools()}
    assert "log_meal" in tools


async def test_unknown_scopes_are_still_rejected(http: httpx.AsyncClient) -> None:
    response = await http.post(
        "/register",
        json={
            "client_name": "Other",
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "scope": "nutrition admin",
        },
    )
    assert response.status_code == 400
