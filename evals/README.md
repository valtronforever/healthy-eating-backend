# MCP evaluation suite

Checks how a real model uses the HealthyEating MCP tools. Unit tests cover the server
logic; this suite covers what only an LLM can show: whether the tool descriptions, server
instructions and tool results make the assistant save complete data, keep the user's
existing workflow unchanged and tell the user whether the meal was saved.

It runs on demand, not in `pytest`: it needs a model, costs tokens and its results vary
between runs. Run it after changing tool descriptions, server `INSTRUCTIONS`, tool
parameters or tool results.

## Run

Prerequisites: Postgres from `docker compose up -d postgres`, and
[opencode](https://opencode.ai) installed and signed in to the model provider.

```bash
uv run python -m evals                      # all scenarios, 3 runs each, openai/gpt-6-luna
uv run python -m evals --list               # list scenarios
uv run python -m evals --only photo,correction --runs 1
uv run python -m evals --model openai/gpt-6.1-sol
```

A scenario passes only if all its runs pass; the exit code is 0 when everything passes.
A full run takes about two minutes. Each run writes to `evals/runs/<timestamp>/`
(git-ignored): `report.md`, `results.json`, one transcript per scenario run (messages,
replies, tool calls, stored values) and the app and opencode logs.

Options: `--model` (or `EVAL_MODEL`), `--runs`, `--only`, `--database-url` (or
`EVAL_DATABASE_URL`, default `healthy_eating_eval` on the local Postgres; **it is dropped
and recreated on every run**). Never start a second run while one is still going: both use the same database, and the
second run's reset breaks the first (seen as a failed run and an opencode warm-up that hangs), `--opencode` (binary), `--turn-timeout`.

## How it works

1. Creates the `healthy_eating_eval` database (if needed), recreates the schema, and adds
   a test user with an access token directly in the database (OAuth is covered by unit
   tests).
2. Starts the app with uvicorn on a free port.
3. Starts a recording proxy (`proxy.py`) in front of `/mcp`. It records every
   `tools/call` with its arguments and result, and can make tool calls fail with HTTP 503.
4. Starts `opencode serve` in an empty temporary folder with a generated config:
   - `healthyeating`: our MCP server through the proxy, with the bearer token, `oauth: false`
     and `codemode: false` (tools are offered to the model directly, as in Claude and
     ChatGPT);
   - `sheets`: a fake Google Sheets connector (`fake_sheets.py`) that records rows;
   - one agent per persona (`personas.py`) with all built-in opencode tools denied, so the
     model only has the apps that persona has connected.
5. Sends a warm-up message until both MCP servers are connected (opencode connects them on
   first use; without the warm-up the first message would reach the model without tools).
6. For each scenario run: clears the meals, the sheet and the proxy log, talks to the model
   with `opencode run --server ... --format json` (multi-turn via `--session`), then checks
   the database, the recorded tool calls and the reply.

## Scenarios

| Scenario | What must happen |
|---|---|
| `numbers_from_conversation` | Numbers the user calculated are stored unchanged, `source=from_conversation`, nothing marked estimated, user told it was saved |
| `photo` | Meal photo (`fixtures/breakfast.jpg`): the reply includes an estimate; if the assistant asks a follow-up, the eval answers it; one meal saved with `estimated_by_assistant` and plausible values |
| `existing_workflow` | "Calorie project" persona: answer keeps the project's format, the Sheets row is still added (not after HealthyEating), the same numbers reach HealthyEating |
| `missing_portion` | "I just had some soup": a follow-up question (a rough estimate may be saved right away); after the answer exactly one meal, updated in place, and no new question |
| `correction` | A correction updates the same meal via `replaces_meal_id`; no duplicate |
| `earlier_meal` | "Yesterday evening I had ...": saved with the user's numbers, dated yesterday (not "now"), and the user is never asked for a UTC offset or time zone |
| `server_error` | Tool calls fail (proxy returns 503): nothing saved, the user is told it was not saved, no false "saved" claim |
| `time_zone` | "I live in Berlin. Breakfast this morning at 8 ...": `set_timezone` stores `Europe/Berlin`, one meal saved with the user's numbers, no question about the time zone (notes: called before `log_meal`, stored at 08:00 Berlin time) |
| `day_complete` | Meal with numbers plus "That's all I'm eating today": one meal saved and today marked complete by the assistant, without asking |
| `what_did_i_eat` | Two meals already saved today: the assistant reads them (`get_daily_summary` or `list_meals`), gives the 830 kcal total, logs and deletes nothing, and makes no judgment about the intake |
| `delete_duplicate` | The same oatmeal saved twice: `delete_meal` is called once, one copy remains, the user is told it was removed |
| `not_food` | A nutrition question that is not a meal logs nothing |

Checks are either required (decide pass or fail) or notes (reported only), for example
whether the assistant repeated the tool's `user_message` word for word.

To add a scenario, write an async function in `scenarios.py` that talks through
`env.chat(...)` and records checks with `checks.require(...)` / `checks.note(...)`, then
add it to `SCENARIOS`.

## Limits

- opencode with a GPT model is not the Claude or ChatGPT app: their system prompts and
  tool handling differ. Passing here is necessary, not sufficient; check important prompt
  changes in the real apps too.
- Eval conversations are stored in opencode's session history on your machine (synthetic
  data only).
- Results vary between runs and models; a single failure is worth reading in the
  transcript before changing prompts.
