import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.mime import UI_MIME_TYPE

from healthy_eating.mcp_server.tools.meals import LogMealResult
from healthy_eating.mcp_server.ui import CARD_META_KEY, MEAL_CARD_URI, card_html
from healthy_eating.mcp_server.ui.build_tokens import OUTPUT, build_css
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, register_client, sign_in
from tests.test_meals import COMPLETE_MEAL

IOS_TOKENS = Path(__file__).parents[2] / "healthy-eating-ios" / "design" / "system" / "tokens.json"


@pytest.fixture
async def access_token(http: httpx.AsyncClient) -> str:
    client = await register_client(http, name="Claude")
    tokens = await sign_in(http, client, email="card@example.com")
    token: str = tokens["access_token"]
    return token


@pytest.fixture
def mcp(app: Any, access_token: str) -> Client[StreamableHttpTransport]:
    return mcp_client(app, access_token)


async def log_meal(
    mcp: Client[StreamableHttpTransport], arguments: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    async with mcp:
        result = await mcp.call_tool("log_meal", arguments)
    assert result.structured_content is not None
    assert result.meta is not None
    return result.structured_content, result.meta[CARD_META_KEY]


async def test_log_meal_links_the_meal_card(mcp: Client[StreamableHttpTransport]) -> None:
    async with mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["log_meal"].meta or {}
    assert meta["ui"]["resourceUri"] == MEAL_CARD_URI
    # The card changes nothing for the model: the output schema is the one FastMCP derives
    # from LogMealResult.
    plain = FastMCP("plain")

    @plain.tool
    def log_meal() -> LogMealResult:
        raise NotImplementedError

    async with Client(plain) as client:
        [expected] = await client.list_tools()
    assert tools["log_meal"].output_schema == expected.output_schema


async def test_meal_card_is_a_self_contained_mcp_app(mcp: Client[StreamableHttpTransport]) -> None:
    async with mcp:
        [content] = await mcp.read_resource(MEAL_CARD_URI)
    assert content.mime_type == UI_MIME_TYPE
    html = getattr(content, "text", "")
    assert html == card_html("meal_card.html")
    assert "--he-accent:" in html
    assert "__TOKENS__" not in html
    # Nothing is loaded from the network: no external scripts, styles, images or requests.
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:""", html)
    assert "@import" not in html
    assert "fetch(" not in html
    assert "XMLHttpRequest" not in html
    # Tool data is shown as text, never parsed as HTML.
    assert "innerHTML" not in html


def test_meal_card_does_not_depend_on_one_host_channel() -> None:
    html = card_html("meal_card.html")
    # The standard MCP Apps result and ChatGPT's own globals both fill the card, because some
    # ChatGPT clients deliver only one of them.
    assert "ui/notifications/tool-result" in html
    for name in ("window.openai", "toolOutput", "toolResponseMetadata", "openai:set_globals"):
        assert name in html
    # Without any result the card shows a static note, never a status that waits forever.
    assert "Saving" not in html
    assert 'class="fallback"' in html


async def test_log_meal_card_has_the_day_so_far(
    http: httpx.AsyncClient, mcp: Client[StreamableHttpTransport], access_token: str
) -> None:
    await http.patch(
        "/api/v1/me/settings",
        json={"targets": {"calories_kcal": {"min": 1800, "max": 2100}, "protein_g": {"min": 120}}},
        headers=bearer(access_token),
    )
    await log_meal(mcp, COMPLETE_MEAL)
    content, card = await log_meal(mcp, {**COMPLETE_MEAL, "dish_name": "Apple", "fat_g": 0})

    assert content["status"] == "logged"
    assert card["updated"] is False
    assert card["calm_mode"] is False
    assert card["energy_unit"] == "kcal"
    assert card["local_date"] == card["today"]
    assert card["eaten_at_local"].startswith(card["today"])
    assert card["day_totals"] == {
        "meal_count": 2,
        "calories_kcal": 960,
        "protein_g": 70,
        "fat_g": 30,
        "carbohydrates_g": 36,
    }
    assert card["targets"] == {
        "calories_kcal": {"min": 1800, "max": 2100},
        "protein_g": {"min": 120, "max": None},
    }


async def test_log_meal_card_follows_calm_mode(
    http: httpx.AsyncClient, mcp: Client[StreamableHttpTransport], access_token: str
) -> None:
    await http.patch(
        "/api/v1/me/settings",
        json={"targets": {"calories_kcal": {"min": 1800, "max": 2100}}, "calm_mode": True},
        headers=bearer(access_token),
    )
    _, card = await log_meal(mcp, COMPLETE_MEAL)

    assert card["calm_mode"] is True
    assert "calories_kcal" not in card["day_totals"]
    assert "calories_kcal" not in card["targets"]


async def test_log_meal_card_marks_a_correction(mcp: Client[StreamableHttpTransport]) -> None:
    content, _ = await log_meal(mcp, COMPLETE_MEAL)
    meal_id = content["meal"]["meal_id"]
    _, card = await log_meal(mcp, {**COMPLETE_MEAL, "replaces_meal_id": meal_id, "fat_g": 25})

    assert card["updated"] is True
    assert card["day_totals"]["meal_count"] == 1
    assert card["day_totals"]["fat_g"] == 25


async def test_meal_not_saved_has_no_card_data(mcp: Client[StreamableHttpTransport]) -> None:
    content, card = await log_meal(
        mcp, {"dish_name": "Pasta", "source": "estimated_by_assistant", "calories_kcal": 600}
    )

    assert content["status"] == "needs_more_info"
    assert card == {}


def test_tokens_css_is_built_from_the_ios_tokens() -> None:
    if not IOS_TOKENS.exists():
        pytest.skip("the iOS repository is not checked out next to this one")
    tokens = json.loads(IOS_TOKENS.read_text(encoding="utf-8"))
    assert OUTPUT.read_text(encoding="utf-8") == build_css(tokens)
