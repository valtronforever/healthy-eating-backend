"""One error format for the REST API: {"code", "message", "details"}.

Only paths under /api/ use it; the login pages and the OAuth endpoints keep their own formats.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

API_PREFIX = "/api/"

ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
}


class ErrorDetail(BaseModel):
    field: str | None = Field(description="The invalid input, e.g. 'calories_kcal' or 'from'")
    message: str


class ErrorResponse(BaseModel):
    code: str = Field(
        description=(
            "Machine-readable: bad_request, unauthorized, forbidden, not_found, conflict, "
            "invalid_request or error"
        )
    )
    message: str = Field(description="Human-readable explanation")
    details: list[ErrorDetail] = Field(
        default_factory=list, description="Per-field problems for invalid_request"
    )


def error_response(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


def _json(status_code: int, error: ErrorResponse, headers: Mapping[str, str] | None) -> Response:
    return JSONResponse(error.model_dump(mode="json"), status_code=status_code, headers=headers)


async def api_http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    if not request.url.path.startswith(API_PREFIX):
        return await http_exception_handler(request, exc)
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    error = ErrorResponse(code=ERROR_CODES.get(exc.status_code, "error"), message=message)
    return _json(exc.status_code, error, exc.headers)


def _field(location: tuple[Any, ...]) -> str | None:
    # Drop where the value came from (body, query, path); keep the field path.
    parts = [str(part) for part in location[1:]] if len(location) > 1 else []
    return ".".join(parts) or None


async def api_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    if not request.url.path.startswith(API_PREFIX):
        return await request_validation_exception_handler(request, exc)
    details = [
        ErrorDetail(field=_field(tuple(error.get("loc", ()))), message=str(error.get("msg", "")))
        for error in exc.errors()
    ]
    error = ErrorResponse(
        code="invalid_request", message="The request is not valid", details=details
    )
    return _json(422, error, None)
