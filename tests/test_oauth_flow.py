import httpx

from tests.oauth_helpers import (
    bearer,
    dev_login,
    exchange_code,
    pkce_pair,
    query_params,
    refresh,
    register_client,
    sign_in,
    start_authorization,
)


def assert_invalid_grant(response: httpx.Response) -> None:
    # FastMCP answers invalid_grant with 401, as the MCP spec requires for invalid tokens.
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_grant"


async def test_discovery_metadata(http: httpx.AsyncClient) -> None:
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == "http://localhost/"
    assert metadata["registration_endpoint"] == "http://localhost/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]

    resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["resource"] == "http://localhost/mcp"
    assert resource["authorization_servers"] == ["http://localhost/"]


async def test_registration_accepts_native_and_loopback_redirects(
    http: httpx.AsyncClient,
) -> None:
    await register_client(http, redirect_uri="healthyeating://oauth/callback")
    await register_client(http, redirect_uri="http://localhost:6274/oauth/callback")


async def test_registration_rejects_plain_http_redirect(http: httpx.AsyncClient) -> None:
    response = await http.post(
        "/register",
        json={
            "redirect_uris": ["http://evil.example.com/callback"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_sign_in_returns_tokens_for_rest_api(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    tokens = await sign_in(http, client, email="Alice@Example.com")
    assert tokens["token_type"] == "Bearer"
    # No scope requested: the defaults, nutrition plus offline_access.
    assert tokens["scope"] == "nutrition offline_access"

    me = await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"
    assert me.json()["linked_logins"] == ["dev"]


async def test_rest_api_requires_valid_token(http: httpx.AsyncClient) -> None:
    assert (await http.get("/api/v1/me")).status_code == 401
    response = await http.get("/api/v1/me", headers=bearer("not-a-token"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_same_social_identity_maps_to_same_user(http: httpx.AsyncClient) -> None:
    mcp_client = await register_client(http, name="Claude")
    ios_client = await register_client(http, redirect_uri="healthyeating://oauth/callback")

    first = await sign_in(http, mcp_client, email="bob@example.com")
    second = await sign_in(http, ios_client, email="bob@example.com")
    other = await sign_in(http, ios_client, email="carol@example.com")

    ids = []
    for tokens in (first, second, other):
        me = await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))
        ids.append(me.json()["id"])
    assert ids[0] == ids[1]
    assert ids[0] != ids[2]


async def test_redirect_carries_state(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    _, challenge = pkce_pair()
    await start_authorization(http, client, challenge, state="state-123")
    params = await dev_login(http, "dave@example.com")
    assert params["state"] == "state-123"
    assert params["code"]


async def test_authorization_code_is_single_use(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    verifier, challenge = pkce_pair()
    await start_authorization(http, client, challenge)
    params = await dev_login(http, "erin@example.com")

    assert (await exchange_code(http, client, params["code"], verifier)).status_code == 200
    second = await exchange_code(http, client, params["code"], verifier)
    assert_invalid_grant(second)


async def test_wrong_pkce_verifier_is_rejected(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    _, challenge = pkce_pair()
    await start_authorization(http, client, challenge)
    params = await dev_login(http, "frank@example.com")
    wrong_verifier, _ = pkce_pair()
    response = await exchange_code(http, client, params["code"], wrong_verifier)
    assert_invalid_grant(response)


async def test_refresh_token_rotation_and_reuse_detection(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    tokens = await sign_in(http, client)

    rotated = await refresh(http, client, tokens["refresh_token"])
    assert rotated.status_code == 200
    new_tokens = rotated.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]
    me = await http.get("/api/v1/me", headers=bearer(new_tokens["access_token"]))
    assert me.status_code == 200

    # Reusing the rotated token ends the whole grant, including the new tokens.
    assert_invalid_grant(await refresh(http, client, tokens["refresh_token"]))
    assert_invalid_grant(await refresh(http, client, new_tokens["refresh_token"]))
    me = await http.get("/api/v1/me", headers=bearer(new_tokens["access_token"]))
    assert me.status_code == 401


async def test_revoke_access_token(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    tokens = await sign_in(http, client)
    response = await http.post(
        "/revoke",
        # The MCP SDK requires the client_secret field even for public clients; send it empty.
        data={"token": tokens["access_token"], "client_id": client.client_id, "client_secret": ""},
    )
    assert response.status_code == 200
    me = await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 401
    assert_invalid_grant(await refresh(http, client, tokens["refresh_token"]))


async def test_cancel_login_returns_access_denied(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    _, challenge = pkce_pair()
    await start_authorization(http, client, challenge, state="s1")
    response = await http.get("/login/cancel")
    assert response.status_code == 303
    params = query_params(response.headers["location"])
    assert params == {"error": "access_denied", "state": "s1"}


async def test_login_page_rejects_unknown_request(http: httpx.AsyncClient) -> None:
    response = await http.get("/login", params={"request": "does-not-exist"})
    assert response.status_code == 400


async def test_login_page_shows_client_and_dev_login(http: httpx.AsyncClient) -> None:
    client = await register_client(http, name="Claude Desktop")
    _, challenge = pkce_pair()
    login_url = await start_authorization(http, client, challenge)
    page = await http.get(login_url)
    assert "Claude Desktop" in page.text
    assert "127.0.0.1" in page.text
    assert "Sign in (dev)" in page.text
