"""The OpenAPI contract of the REST API (/api/v1), consumed by the iOS app.

The committed snapshot `openapi.json` must match it (tests/test_openapi.py). After changing the
API, regenerate it with:  uv run python -m healthy_eating.api.openapi > openapi.json
"""

import json
from functools import cache
from typing import Any

from fastapi.openapi.utils import get_openapi

from healthy_eating.api import v1

# Version of the contract, not of the server: bump the minor version for additions and
# introduce /api/v2 for breaking changes.
API_VERSION = "1.0.0"

DESCRIPTION = """\
REST API of a self-hosted Healthy Eating server, used by the iOS app.

Authenticate with an access token from the server's OAuth 2.1 flow. Errors use one format:
`{"code", "message", "details"}`. Dates are the user's local days (YYYY-MM-DD); times are
ISO 8601 with a UTC offset. Energy is in kcal, macronutrients and portions in grams.
"""


@cache
def v1_openapi() -> dict[str, Any]:
    return get_openapi(
        title="Healthy Eating API",
        version=API_VERSION,
        description=DESCRIPTION,
        routes=v1.router.routes,
    )


def snapshot() -> str:
    return json.dumps(v1_openapi(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(snapshot(), end="")
