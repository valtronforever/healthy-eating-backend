import uuid
from pathlib import Path

import httpx
from fastapi import Request
from starlette.exceptions import HTTPException

from healthy_eating.api.errors import api_http_error
from healthy_eating.api.openapi import snapshot, v1_openapi
from tests.oauth_helpers import bearer, register_client, sign_in

SNAPSHOT = Path(__file__).parent.parent / "openapi.json"
METHODS = {"get", "put", "post", "patch", "delete"}


def operations() -> list[tuple[str, str, dict[str, object]]]:
    spec = v1_openapi()
    return [
        (method, path, operation)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


def test_committed_snapshot_matches_the_api() -> None:
    assert SNAPSHOT.read_text(encoding="utf-8") == snapshot(), (
        "The /api/v1 contract changed. If intended, regenerate the snapshot: "
        "uv run python -m healthy_eating.api.openapi > openapi.json"
    )


def test_every_operation_is_explicit_and_documented() -> None:
    ids = [operation["operationId"] for _, _, operation in operations()]
    assert len(ids) == len(set(ids))
    assert all(isinstance(op_id, str) and "_" not in op_id for op_id in ids)
    for method, path, operation in operations():
        assert path.startswith("/api/v1/"), path
        assert operation["security"] == [{"BearerAuth": []}], (method, path)
        responses = operation["responses"]
        assert isinstance(responses, dict)
        for status, response in responses.items():
            if status.startswith("4"):
                schema = response["content"]["application/json"]["schema"]
                assert schema == {"$ref": "#/components/schemas/ErrorResponse"}, (path, status)
    assert "HTTPValidationError" not in v1_openapi()["components"]["schemas"]


async def test_spec_is_served(http: httpx.AsyncClient) -> None:
    response = await http.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert response.json() == v1_openapi()


async def test_errors_use_one_format(http: httpx.AsyncClient) -> None:
    client = await register_client(http)
    headers = bearer((await sign_in(http, client))["access_token"])

    not_found = await http.get(f"/api/v1/meals/{uuid.uuid4()}", headers=headers)
    assert not_found.status_code == 404
    assert not_found.json() == {"code": "not_found", "message": "Meal not found", "details": []}

    unauthorized = await http.get("/api/v1/me")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "unauthorized"
    assert unauthorized.headers["www-authenticate"].startswith("Bearer")

    invalid = await http.patch(
        f"/api/v1/meals/{uuid.uuid4()}", json={"protein_g": -1}, headers=headers
    )
    assert invalid.status_code == 422
    body = invalid.json()
    assert body["code"] == "invalid_request"
    assert [detail["field"] for detail in body["details"]] == ["protein_g"]

    bad_query = await http.get("/api/v1/meals", params={"limit": 0}, headers=headers)
    assert [detail["field"] for detail in bad_query.json()["details"]] == ["limit"]

    bad_range = await http.get(
        "/api/v1/days", params={"from": "2026-10-18", "to": "2026-10-17"}, headers=headers
    )
    assert bad_range.json() == {
        "code": "bad_request",
        "message": "`from` is after `to`",
        "details": [],
    }


async def test_other_routes_keep_their_error_format(http: httpx.AsyncClient) -> None:
    login = await http.get("/login")
    assert login.status_code == 400
    assert login.headers["content-type"].startswith("text/html")

    outside = Request({"type": "http", "path": "/health", "headers": []})
    response = await api_http_error(outside, HTTPException(503, detail="Database unavailable"))
    assert bytes(response.body) == b'{"detail":"Database unavailable"}'
