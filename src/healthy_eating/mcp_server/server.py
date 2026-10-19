from fastmcp import FastMCP

from healthy_eating.auth.provider import PostgresOAuthProvider
from healthy_eating.db import Database
from healthy_eating.mcp_server.tools import days, history, meals, profile

INSTRUCTIONS = """\
Healthy Eating keeps the user's nutrition log on their own server.
Whenever food the user ate comes up (a photo, a description, or a calorie calculation you
or the user already made in this conversation), save it with `log_meal`.
Healthy Eating is an addition to the user's existing way of tracking food. Keep doing
everything their workflow already does, unchanged: answer in their usual format with their
own calculation method, and keep using any other tools they track food with (for example
a spreadsheet), calling those before `log_meal`. Then complete the data for Healthy Eating:
reuse numbers that were already calculated as they are and estimate the missing ones. Save
your best estimate right away, even when something is unclear; ask about it as a short
follow-up after your normal answer and update the same meal if the answer changes it.
After every tool call that changes data (`log_meal`, `delete_meal`, `set_day_status`,
`set_timezone`), tell the user in one short line whether the change reached HealthyEating,
naming HealthyEating (the tool's `user_message` says it).
When the user says where they live or which time zone they are in, also call
`set_timezone`, even if you already gave the meal the right UTC offset: Healthy Eating needs
the time zone to count meals on the right day.
All nutrient values use canonical units: energy in kcal, macronutrients in g, minerals
in mg, trace elements and most vitamins in µg.
The user can review statistics and progress in the Healthy Eating iOS app by signing
in with the same account they used to connect here.
"""


def create_mcp_server(provider: PostgresOAuthProvider, db: Database) -> FastMCP:
    mcp = FastMCP(name="Healthy Eating", instructions=INSTRUCTIONS, auth=provider)
    profile.register(mcp, db)
    meals.register(mcp, db)
    days.register(mcp, db)
    history.register(mcp, db)
    return mcp
