from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, update

from healthy_eating.db.base import utcnow
from healthy_eating.db.models import LinkRequest
from healthy_eating.services.cleanup import delete_expired
from tests.oauth_helpers import pkce_pair, query_params
from tests.test_meals_api import Account, account


async def logins(http: httpx.AsyncClient, owner: Account) -> list[dict[str, Any]]:
    response = await http.get("/api/v1/me/logins", headers=owner.headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["logins"]
    return items


async def start_link(http: httpx.AsyncClient, owner: Account, challenge: str) -> str:
    response = await http.post(
        "/api/v1/me/logins/link", json={"code_challenge": challenge}, headers=owner.headers
    )
    assert response.status_code == 201, response.text
    url = urlparse(response.json()["url"])
    return f"{url.path}?{url.query}"


async def sign_in_to_link(http: httpx.AsyncClient, link_url: str, email: str) -> dict[str, str]:
    """Open the link page and sign in with the dev login; returns the app callback's params."""
    page = await http.get(link_url)
    assert page.status_code == 200, page.text
    assert "Link an account" in page.text
    response = await http.post("/auth/dev-login", data={"email": email})
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("healthyeating://link/callback?")
    return query_params(location)


async def complete(
    http: httpx.AsyncClient, owner: Account, code: str, verifier: str
) -> httpx.Response:
    return await http.post(
        "/api/v1/me/logins/link/complete",
        json={"code": code, "code_verifier": verifier},
        headers=owner.headers,
    )


async def test_a_linked_login_signs_in_to_the_same_account(http: httpx.AsyncClient) -> None:
    owner = await account(http, "ann.link@example.com")
    verifier, challenge = pkce_pair()

    params = await sign_in_to_link(
        http, await start_link(http, owner, challenge), "ann.work@example.com"
    )
    response = await complete(http, owner, params["code"], verifier)
    again = await complete(http, owner, params["code"], verifier)

    assert response.status_code == 200, response.text
    assert response.json() == {"result": "linked"}
    assert again.status_code == 400
    assert [(login["provider"], login["email"]) for login in await logins(http, owner)] == [
        ("dev", "ann.link@example.com"),
        ("dev", "ann.work@example.com"),
    ]
    other_login = await account(http, "ann.work@example.com")
    assert other_login.user_id == owner.user_id
    me = (await http.get("/api/v1/me", headers=owner.headers)).json()
    assert me["linked_logins"] == ["dev", "dev"]


async def test_a_login_of_the_same_account_is_already_linked(http: httpx.AsyncClient) -> None:
    owner = await account(http, "ben.link@example.com")
    verifier, challenge = pkce_pair()

    params = await sign_in_to_link(
        http, await start_link(http, owner, challenge), "ben.link@example.com"
    )
    response = await complete(http, owner, params["code"], verifier)

    assert response.json() == {"result": "already_linked"}
    assert len(await logins(http, owner)) == 1


async def test_a_login_of_another_account_is_refused_and_never_merged(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "cat.link@example.com")
    other = await account(http, "cat.other@example.com")
    verifier, challenge = pkce_pair()

    params = await sign_in_to_link(
        http, await start_link(http, owner, challenge), "cat.other@example.com"
    )
    refused = await complete(http, owner, params["code"], verifier)
    reused = await complete(http, owner, params["code"], verifier)

    assert refused.status_code == 409
    assert refused.json()["code"] == "conflict"
    # The code is used up although the request was refused.
    assert reused.status_code == 400
    assert len(await logins(http, owner)) == 1
    assert len(await logins(http, other)) == 1


async def test_a_link_finishes_only_with_the_verifier_of_the_app_that_started_it(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "dan.link@example.com")
    verifier, challenge = pkce_pair()
    wrong_verifier, _ = pkce_pair()

    params = await sign_in_to_link(
        http, await start_link(http, owner, challenge), "dan.work@example.com"
    )
    wrong = await complete(http, owner, params["code"], wrong_verifier)
    right_too_late = await complete(http, owner, params["code"], verifier)

    assert wrong.status_code == 400
    assert right_too_late.status_code == 400
    assert len(await logins(http, owner)) == 1


async def test_another_user_cannot_finish_someone_elses_link(http: httpx.AsyncClient) -> None:
    owner = await account(http, "eve.link@example.com")
    stranger = await account(http, "eve.stranger@example.com")
    verifier, challenge = pkce_pair()

    params = await sign_in_to_link(
        http, await start_link(http, owner, challenge), "eve.work@example.com"
    )
    response = await complete(http, stranger, params["code"], verifier)

    assert response.status_code == 400
    assert len(await logins(http, stranger)) == 1
    assert len(await logins(http, owner)) == 1


async def test_cancelled_and_expired_links_go_back_to_the_app(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "fay.link@example.com")
    _, challenge = pkce_pair()
    link_url = await start_link(http, owner, challenge)

    assert (await http.get(link_url)).status_code == 200
    cancel = await http.get("/link/cancel")
    assert cancel.status_code == 303
    assert cancel.headers["location"] == "healthyeating://link/callback?error=cancelled"

    unknown = await http.get("/link?request=nope")
    assert "invalid or has expired" in unknown.text

    assert (await http.get(link_url)).status_code == 200
    async with app.state.db.session() as session:
        await session.execute(
            update(LinkRequest).values(expires_at=utcnow() - timedelta(seconds=1))
        )
    late = await http.post("/auth/dev-login", data={"email": "fay.work@example.com"})
    assert late.headers["location"] == "healthyeating://link/callback?error=expired"
    assert len(await logins(http, owner)) == 1


async def test_logins_can_be_removed_but_never_the_last(http: httpx.AsyncClient) -> None:
    owner = await account(http, "gus.link@example.com")
    stranger = await account(http, "gus.stranger@example.com")
    [only] = await logins(http, owner)

    last = await http.delete(f"/api/v1/me/logins/{only['id']}", headers=owner.headers)
    assert last.status_code == 409

    verifier, challenge = pkce_pair()
    params = await sign_in_to_link(
        http, await start_link(http, owner, challenge), "gus.work@example.com"
    )
    assert (await complete(http, owner, params["code"], verifier)).status_code == 200
    foreign = await http.delete(f"/api/v1/me/logins/{only['id']}", headers=stranger.headers)
    removed = await http.delete(f"/api/v1/me/logins/{only['id']}", headers=owner.headers)

    assert foreign.status_code == 404
    assert removed.status_code == 204
    assert [login["email"] for login in await logins(http, owner)] == ["gus.work@example.com"]


async def test_cleanup_deletes_expired_link_requests(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "hal.link@example.com")
    _, challenge = pkce_pair()
    await start_link(http, owner, challenge)

    async with app.state.db.session() as session:
        await delete_expired(session, now=utcnow())
        kept = await session.scalar(select(func.count()).select_from(LinkRequest))
        await delete_expired(session, now=utcnow() + timedelta(minutes=11))
        left = await session.scalar(select(func.count()).select_from(LinkRequest))

    assert kept == 1
    assert left == 0
