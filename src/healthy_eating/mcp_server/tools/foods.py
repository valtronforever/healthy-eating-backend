"""MCP tool for looking up USDA reference foods (healthy-eating-backend#25)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.nutrition.micronutrients import ItemMatchStatus
from healthy_eating.services.food_matching import search_foods

FIND_FOODS_DESCRIPTION = """\
Look up foods in HealthyEating's USDA reference data by name. Use it when a log_meal item
matched nothing or the wrong food, to pick the right `food_id`, then correct the meal with
log_meal (`replaces_meal_id`, the same values, and the item with that `food_id`). Name the food
plainly, as in a food table: "salmon, baked", "yogurt, greek, plain".
"""


class ReferenceFoodResult(BaseModel):
    food_id: int = Field(description="Pass as the item's food_id in log_meal")
    description: str
    energy_kcal_per_100g: float | None
    match: ItemMatchStatus = Field(description="How well the name matches this food")


class FindFoodsResult(BaseModel):
    foods: list[ReferenceFoodResult] = Field(description="Best match first")


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        name="find_foods",
        description=FIND_FOODS_DESCRIPTION,
        annotations={"title": "Find reference foods", "readOnlyHint": True, "openWorldHint": False},
    )
    async def find_foods(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Food name")],
        limit: Annotated[int, Field(ge=1, le=10, description="How many foods")] = 5,
    ) -> FindFoodsResult:
        async with db.session() as session:
            found = await search_foods(session, query, limit=limit)
        return FindFoodsResult(
            foods=[
                ReferenceFoodResult(
                    food_id=food.id,
                    description=food.description,
                    energy_kcal_per_100g=food.energy_kcal,
                    match=status,
                )
                for food, status in found
            ]
        )
