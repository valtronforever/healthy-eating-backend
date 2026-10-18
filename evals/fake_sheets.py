"""A stand-in for a Google Sheets connector, run by opencode as a local MCP server.

It plays the user's existing food tracking workflow: scenarios check that the assistant
keeps adding rows here exactly as before, even with HealthyEating connected. Every row is
appended as JSON to the file named by FAKE_SHEETS_LOG.
"""

import json
import os
import time
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP(
    name="Google Sheets",
    instructions="The user's food diary spreadsheet. Add one row per meal with append_row.",
)


@mcp.tool(description="Append one row to the 'Food log' sheet of the user's food diary.")
def append_row(
    date: Annotated[str, Field(description="Date of the meal, YYYY-MM-DD")],
    meal: Annotated[str, Field(description="What the user ate")],
    calories_kcal: Annotated[float, Field(description="Energy, kcal")],
    protein_g: Annotated[float, Field(description="Protein, g")],
    fat_g: Annotated[float, Field(description="Fat, g")],
    carbs_g: Annotated[float, Field(description="Carbohydrates, g")],
) -> str:
    row = {
        "ts": time.time(),
        "date": date,
        "meal": meal,
        "calories_kcal": calories_kcal,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
    }
    with open(os.environ["FAKE_SHEETS_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(row) + "\n")
    return "Row added to 'Food log'."


if __name__ == "__main__":
    mcp.run()
