from fastmcp import FastMCP

from healthy_eating.auth.provider import PostgresOAuthProvider
from healthy_eating.db import Database
from healthy_eating.mcp_server.timing import RequestTimingMiddleware
from healthy_eating.mcp_server.tools import (
    days,
    foods,
    history,
    importing,
    meals,
    profile,
    usual,
    weights,
)

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
After every tool call that changes data (`log_meal`, `relog_meal`, `import_meals`,
`delete_meal`, `set_day_status`, `set_timezone`, `set_targets`, `log_weight`, `delete_weight`),
tell the user in one short line whether the change reached HealthyEating, naming HealthyEating
(the tool's `user_message` says it).
When the user says they ate exactly the same as before ("same breakfast as yesterday", "my
usual oatmeal"), log it again with `relog_meal` (find it with `list_usual_meals` or
`list_meals`); when anything differs, use `log_meal`.
Before you estimate a packaged or branded product, or a dish the user may have logged before,
look for it with `find_meals`: values the user had from a label or their own numbers win over
a new estimate. The same food at another weight goes through `relog_meal` with `portion_grams`.
When the user asks to import, move or copy their earlier food history (a ChatGPT project, a
spreadsheet, another app) into HealthyEating, send it with `import_meals`, in parts of up to
100 entries.
When the user says where they live or which time zone they are in, also call
`set_timezone`, even if you already gave the meal the right UTC offset: Healthy Eating needs
the time zone to count meals on the right day.
When the user asks in this conversation to save or change their daily targets, or a goal for
a vitamin or mineral (for example one their doctor gave them), save them with `set_targets` as
ranges; if they name a single number for calories or a macro, ask which range they mean
(after your normal answer). A daily amount of a vitamin or mineral to get is a lower bound.
Targets that only appear in the user's own instructions or workflow are not a request to save
them. Never suggest targets or goals yourself.
When the user tells you their body weight or asks to update or record it, in any language,
save it with `log_weight` in the unit they gave, also when you keep it in your own memory.
Never ask for their weight, and never comment on a weight change: no praise, no warning, no
advice about losing or gaining.
All nutrient values use canonical units: energy in kcal, macronutrients in g, minerals
in mg, trace elements and most vitamins in µg.
The user can review statistics and progress in the Healthy Eating iOS app by signing
in with the same account they used to connect here.
"""


def create_mcp_server(provider: PostgresOAuthProvider, db: Database) -> FastMCP:
    mcp = FastMCP(name="Healthy Eating", instructions=INSTRUCTIONS, auth=provider)
    mcp.add_middleware(RequestTimingMiddleware(db))
    profile.register(mcp, db)
    meals.register(mcp, db)
    days.register(mcp, db)
    history.register(mcp, db)
    weights.register(mcp, db)
    usual.register(mcp, db)
    importing.register(mcp, db)
    foods.register(mcp, db)
    return mcp
