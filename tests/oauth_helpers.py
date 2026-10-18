"""Drive the OAuth flow the way an MCP client or the iOS app does, using the dev login."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

REDIRECT_URI = "http://127.0.0.1:33418/callback"
MCP_RESOURCE = "http://localhost/mcp"


@dataclass
class Client:
    client_id: str
    redirect_uri: str


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def register_client(
    http: httpx.AsyncClient, redirect_uri: str = REDIRECT_URI, name: str = "Test Client"
) -> Client:
    response = await http.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return Client(client_id=response.json()["client_id"], redirect_uri=redirect_uri)


async def start_authorization(
    http: httpx.AsyncClient, client: Client, challenge: str, state: str = "xyz"
) -> str:
    """Call /authorize and open the login page. Returns the login page URL."""
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": MCP_RESOURCE,
        },
    )
    assert response.status_code == 302, response.text
    login_url = response.headers["location"]
    page = await http.get(login_url)
    assert page.status_code == 200, page.text
    return login_url


def query_params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


async def dev_login(http: httpx.AsyncClient, email: str) -> dict[str, str]:
    """Submit the dev login form. Returns the query params of the client redirect."""
    response = await http.post("/auth/dev-login", data={"email": email})
    assert response.status_code == 303, response.text
    return query_params(response.headers["location"])


async def exchange_code(
    http: httpx.AsyncClient, client: Client, code: str, verifier: str
) -> httpx.Response:
    return await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client.redirect_uri,
            "client_id": client.client_id,
            "code_verifier": verifier,
        },
    )


async def refresh(http: httpx.AsyncClient, client: Client, refresh_token: str) -> httpx.Response:
    return await http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client.client_id,
        },
    )


async def sign_in(
    http: httpx.AsyncClient, client: Client, email: str = "alice@example.com"
) -> dict[str, Any]:
    """Full flow: authorize, dev login, code exchange. Returns the token response."""
    verifier, challenge = pkce_pair()
    await start_authorization(http, client, challenge)
    params = await dev_login(http, email)
    response = await exchange_code(http, client, params["code"], verifier)
    assert response.status_code == 200, response.text
    tokens: dict[str, Any] = response.json()
    return tokens


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
