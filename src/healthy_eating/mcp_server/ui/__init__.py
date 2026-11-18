"""Cards shown in the chat (MCP Apps, extension `io.modelcontextprotocol/ui`).

A tool links a `ui://` resource in its `_meta.ui.resourceUri`; clients that support MCP Apps
render that HTML in a sandboxed iframe next to the tool call and pass it the tool result.
Clients without MCP Apps ignore it and keep using the tool's text result, which never changes
because of a card.

The cards are self-contained: tokens, styles and script are inlined, they load nothing from
the network (empty CSP) and only show data the tool result carries. Card-only data (the day's
totals, targets, Calm mode, the energy unit) travels in the result's `_meta` under
`CARD_META_KEY`, never in `structuredContent`, so the model's view of a tool stays the same.
"""

from functools import cache
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.tools import Tool, ToolResult
from pydantic import BaseModel

UI_DIR = Path(__file__).parent
MEAL_CARD_URI = "ui://healthy-eating/meal-card.html"
CARD_META_KEY = "healthyeating/card"
_TOKENS_PLACEHOLDER = "/*__TOKENS__*/"


@cache
def card_html(name: str) -> str:
    """The card's HTML with the design tokens inlined."""
    page = (UI_DIR / name).read_text(encoding="utf-8")
    tokens = (UI_DIR / "tokens.css").read_text(encoding="utf-8")
    return page.replace(_TOKENS_PLACEHOLDER, tokens, 1)


def card_tool_app(resource_uri: str) -> AppConfig:
    """The `app` setting of a tool that shows a card (the model still calls the tool)."""
    return AppConfig(resource_uri=resource_uri, visibility=["model", "app"])


class CardResult:
    """Builds tool results that keep a model's usual output and add card data in `_meta`.

    FastMCP derives a tool's output schema and text content from its return type. A card tool
    returns `ToolResult` instead (the only way to set `_meta`), so this keeps both exactly as
    they were for `model`.
    """

    def __init__(self, model: type[BaseModel]) -> None:
        def shape() -> None: ...

        shape.__annotations__["return"] = model
        self._shape = Tool.from_function(shape)

    @property
    def output_schema(self) -> dict[str, Any] | None:
        return self._shape.output_schema

    def __call__(self, result: BaseModel, card: dict[str, Any]) -> ToolResult:
        tool_result = self._shape.convert_result(result)
        tool_result.meta = {CARD_META_KEY: card}
        return tool_result


def register(mcp: FastMCP) -> None:
    @mcp.resource(
        MEAL_CARD_URI,
        name="meal_card",
        title="Saved meal",
        description="Shows a meal saved to HealthyEating, with the day so far",
        app=AppConfig(csp=ResourceCSP(), prefers_border=False),
    )
    def meal_card() -> str:
        return card_html("meal_card.html")
