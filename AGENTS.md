# AGENTS.md: healthy-eating-backend

Guidance for AI coding agents and contributors working in this repository.

## Project overview

Healthy Eating is an open-source (MIT), self-hostable app for tracking nutrition. It has two parts:

- **healthy-eating-backend** (this repo): a Python service that exposes an **MCP server** with tools for logging food and computing calories, macronutrients, vitamins and minerals. It also provides a **REST API** for the iOS app and an **OAuth 2.1 authorization server** with social login. Data is stored in **PostgreSQL**.
- **healthy-eating-ios** (separate repo): a SwiftUI app that shows statistics and progress.

The LLM in the user's MCP client (Claude, ChatGPT, etc.) recognizes the food and estimates portions. The backend does **not** call an LLM. Its tools accept structured input (food items + amounts), look up nutrient data, compute totals and persist them.

Status: early v1. Implemented: OAuth 2.1 server with social and dev login, MCP endpoint with `get_profile`, `set_timezone`, `set_targets`, `log_meal` (meals stored in the `meals` table), `set_day_status`, `list_meals`, `get_daily_summary`, `delete_meal`, `log_weight`, `list_weights` and `delete_weight` tools, REST `/api/v1/me`, `/api/v1/me/settings`, `/api/v1/meals` (list, get, correct, delete), `/api/v1/days` (daily summaries), `/api/v1/days/{date}/status`, `/api/v1/trends`, `/api/v1/weekly-review`, `/api/v1/weights` (list with trend, add one or a batch, correct, delete, delete all), `/api/v1/expenditure`, `/api/v1/integration-status` and `/api/v1/connections`, Docker dev stack, tests. Production setup: `compose.production.yml` and `docs/self-hosting.md`. Keep this file in sync when the layout or commands change.

## Hard constraints

1. **Open source only.** Every dependency must use an MIT-compatible license (MIT, BSD, Apache-2.0, ISC, PSF). No GPL/AGPL/SSPL, no proprietary SDKs, no required SaaS. Check the license before adding a dependency.
2. **Self-hostable with minimal moving parts.** A deployment is this app + PostgreSQL (+ optional reverse proxy). Do not add services such as Redis, Keycloak or message queues without an explicit decision.
3. **All configuration via environment variables** (documented in `.env.example`). Features with external credentials, like each social provider, are enabled only when their variables are set.
4. **Health data is sensitive** (likely GDPR Art. 9). Never log meal contents, nutrient values, tokens or emails. Support full account export and deletion: `GET /api/v1/export` (format in `docs/export-format.md`; add new user data to it) and `DELETE /api/v1/me` relies on `ON DELETE CASCADE` from every user-owned table to `users`: give each new user-owned table that foreign key (`tests/test_delete_account.py` checks every table with a `user_id` column).

## Tech stack

| Concern | Choice |
|---|---|
| Language / tooling | Python 3.13+, `uv`, `ruff` (lint + format), `mypy` (strict), `pytest` + `pytest-asyncio` |
| Web framework | FastAPI (async everywhere) |
| MCP | FastMCP (Streamable HTTP transport), mounted into the FastAPI app |
| Our OAuth 2.1 server | FastMCP `OAuthProvider` subclass (built on `mcp.server.auth`), backed by Postgres |
| Social login (upstream) | Authlib `starlette_client` (OIDC): Google, Microsoft, Apple |
| JWT / JOSE | `joserfc` (also used to sign the Apple client secret) |
| DB | PostgreSQL, SQLAlchemy 2.0 async + `asyncpg`, Alembic migrations |
| Templates | Jinja2 (login / provider picker / consent pages only) |
| Deployment | Docker image + `docker-compose.yml` (app + postgres) |

## Target layout

```
src/healthy_eating/
  main.py            # create_app() factory; mounts MCP + OAuth endpoints, login pages, REST API
  config.py          # Settings (pydantic-settings), reads env vars / .env
  db/                # Database (engine + sessions), Base, SQLAlchemy models
  auth/
    provider.py      # PostgresOAuthProvider: our OAuth 2.1 authorization server
    social.py        # Authlib clients for google / microsoft / apple
    routes.py        # /login, /login/cancel, /auth/start/{provider}, /auth/callback/{provider}, /auth/dev-login
    tokens.py        # token generation and hashing
    templates/       # login and error pages (Jinja2)
  mcp_server/        # named so it does not shadow the `mcp` SDK package
    server.py        # FastMCP instance + instructions + tool registration
    context.py       # current_user_id() for tools
    timing.py        # logs each MCP request: method, tool, duration, session and client (never arguments)
    tools/           # one module per tool group, each exposing register(mcp, db)
  api/
    deps.py          # SessionDep, CurrentUserId (bearer token auth)
    errors.py        # ErrorResponse {code, message, details} for everything under /api/
    openapi.py       # the /api/v1 contract; `python -m healthy_eating.api.openapi` prints it
    health.py        # /health
    v1/              # REST routers for the iOS app
  services/          # business logic shared by MCP tools and REST
  nutrition/         # shared nutrition types; later reference data, unit conversion
migrations/          # Alembic (async), dated revision files
docker/postgres/init # creates the test database on first start
tests/               # pytest, real Postgres; tests/oauth_helpers.py drives the OAuth flow
evals/               # MCP evaluation suite: real model via opencode, run on demand (evals/README.md)
docs/                # for users and self-hosters: self-hosting.md, export-format.md
openapi.json         # committed snapshot of the /api/v1 contract (tests fail if it drifts)
docker-compose.yml   # local development stack
compose.production.yml  # production: app + Postgres behind any HTTPS proxy (docs/self-hosting.md)
.env.production.example # template for .env.production (git-ignored, holds the secrets)
Dockerfile           # targets: dev (reload) and prod (non-root)
.env.example
```

**Rule:** MCP tools and REST routers are thin. They validate input, resolve the current user, and call `services/`. Business logic never lives in a tool or router.

## Commands

```bash
# Everything in Docker (Postgres + app with auto-reload on http://localhost:8000)
cp .env.example .env
docker compose up --build
docker compose exec app pytest

# Or run the app on the host against the dockerized Postgres
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn --factory healthy_eating.main:create_app --reload

# Checks (all must pass before finishing a change)
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests evals

# MCP evaluation with a real model through opencode (needs opencode signed in; ~2 min)
uv run python -m evals

# After changing the REST API: regenerate the committed contract snapshot
uv run python -m healthy_eating.api.openapi > openapi.json

# New migration after changing models (review the generated file by hand)
uv run alembic revision --autogenerate -m "<message>"
```

Tests need Postgres running (`docker compose up -d postgres`); they use the `healthy_eating_test` database (override with `TEST_DATABASE_URL`).

## Authentication design

Our backend **is the authorization server**. Google, Microsoft and Apple are only used to identify the user. Clients always receive **our** tokens, never upstream tokens.

Flow (identical for MCP clients and the iOS app):

1. Client calls `/authorize` (PKCE required). MCP clients register via Dynamic Client Registration (`/register`). The iOS app uses the same flow through `ASWebAuthenticationSession` with redirect `healthyeating://oauth/callback`.
2. The provider stores a `LoginRequest` and redirects to `/login?request=...`, which binds the request to the browser session and shows the client name, the redirect host, and buttons for the configured providers.
3. Upstream OIDC login via Authlib → callback `/auth/callback/{provider}`.
4. **Find or create** the user by `identities(provider, subject)`. First login from any entry point creates the account (supports both the "app first" and the "MCP first" onboarding).
5. Issue our authorization code → client exchanges it at `/token` for our access + refresh tokens.

Rules:
- Key identities by `(provider, sub)`, never by email.
- Do **not** auto-link accounts across providers by email in v1 (the Microsoft `email` claim is not reliably verified). Linking is an explicit action by a logged-in user.
- Apple: client secret is an ES256 JWT generated on the fly; callback is `form_post` (POST); the user's name is only sent on first login, so persist it then.
- Microsoft: use the `common` endpoint; validate the issuer against the `https://login.microsoftonline.com/{tenantid}/v2.0` template.
- Strict redirect URI validation: `https`, loopback (`http://127.0.0.1` / `localhost`) and the app's custom scheme only.
- Auth codes are short-lived and single-use. All tokens are opaque and stored only as SHA-256 hashes. Every sign-in is a row in `grants`; its tokens share the `grant_id`. Refresh tokens rotate, and presenting a rotated refresh token again revokes the whole grant. Revoke grants only through `services/connections.py:revoke_grant`, so the grant row and all tokens end together.
- Expired sign-in data is deleted inside the app process (`services/cleanup.py`, an asyncio task started in the lifespan, every `CLEANUP_INTERVAL_SECONDS`, default 1 h): login requests, authorization codes, access and refresh tokens past `expires_at`, then grants with no token left. Rotated or revoked refresh tokens stay until they expire, so reuse detection keeps working. OAuth clients are never deleted (meals refer to them).
- Expose `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`. The MCP endpoint and the REST API accept the same bearer tokens.
- Scopes: `nutrition` (every token carries it; the MCP endpoint requires it) and `offline_access` (advertised and accepted because some clients, ChatGPT, only use refresh tokens when it is listed; refresh tokens are issued either way). A sign-in always includes `nutrition`, even when the client asked only for `offline_access`. Clients registered before `offline_access` existed can only request `nutrition`; they register again to get it.
- `DEV_LOGIN_ENABLED` adds an email-only login for local development and tests. `Settings` refuses it in production; never weaken that check.
- Library behavior to keep in mind: FastMCP answers `invalid_grant` with HTTP 401 (MCP spec), the MCP SDK's `/revoke` requires a `client_secret` form field even for public clients (send it empty), and the issuer (`BASE_URL`) must be `https` or `localhost`.

## Domain rules

- Store nutrients in canonical units: energy in `kcal`, macros in `g`, minerals in `mg`, trace elements and most vitamins in `µg`. Convert at the edges only.
- Fiber (`fiber_g`), sugar (`sugar_g`) and sodium (`sodium_mg`) are optional on every meal (NULL when unknown; `OPTIONAL_NUTRIENT_FIELDS`); the required set stays calories and three macros (`NUTRIENT_FIELDS`). They follow the same provenance rule and may appear in `estimated_fields` (`TRACKED_NUTRIENT_FIELDS`). Sugar above carbohydrates is a warning, never an error. Day totals report each as `{amount, meal_count}` and Trends as `{average, days}` (only meals and counted days that have a value), so a partial value never poses as a full one. The weekly review's estimated share counts the values each meal has.
- A logged food entry stores a **snapshot** of its computed nutrient values, so later changes to reference data never rewrite history.
- Corrections (`replaces_meal_id`) update the existing `meals` row in place, so a meal is never counted twice. `eaten_at` is stored in UTC; `utc_offset_minutes` keeps the offset of the meal's local time: the one the user gave, otherwise the one of the user's time zone (`users.timezone`) at that moment, or NULL when neither is known. A time sent without an offset is read in the user's time zone, or stored as UTC with the offset unknown when no time zone is set.
- Corrections from the app (`PATCH /api/v1/meals/{id}`) update the same row through `services/meals.py:edit_meal`. A nutrition value the user changes is no longer estimated; a value sent unchanged keeps its mark. Input limits (`MAX_*` in `services/meals.py`) are shared by `log_meal` and the REST API.
- Day completeness (`services/days.py`): a local day can be marked `complete` or `partial` by the user (app) or the assistant (`set_day_status`). Averages count only complete days with meals once the user has marked any day complete; otherwise every day with meals except partial ones. Days without meals never count. Use `day_counts()` for every average.
- A meal's local day uses its stored offset first, then the user's current time zone, then UTC (`services/local_time.py`). Use these helpers for every per-day query so the REST API and MCP tools agree.
- Store timestamps in UTC plus the user's IANA timezone. Daily aggregates are computed in the user's local day.
- Weekly review (`services/weekly_review.py`, `/api/v1/weekly-review?week=`): a local week runs Monday to Sunday. Averages count days like Trends (today only once complete, never later days) and name every left-out day with its reason; weekdays and weekend use the same rule. Per-meal facts (protein per meal and its 10 g buckets, late meals from 21:00 to 04:00 local time, share of estimated values, low-confidence meals) use every meal of the week. Neutral facts only, no scores.
- Weight (`services/weights.py`, `/api/v1/weights`) is opt-in (decision healthy-eating-ios#14): `users.weight_tracking` is off until the first weigh-in from the chat or the app, or until the user turns it on; Apple Health entries are refused (409) while it is off; turning it off keeps the entries, deleting them is separate (`DELETE /api/v1/weights` with `{"confirm": "delete-all-weights"}`). Stored in kg (`weight_unit` is display only), with `measured_at` in UTC plus offset like meals; Apple Health samples are de-duplicated by `external_id`. Trend: daily mean, then a gap-aware 10 % exponential average from the first weigh-in; weekly change over the last 28 days (at least 14). The method is in the module docstring; keep wording about weight neutral.
- Energy expenditure (`services/expenditure.py`, `/api/v1/expenditure`): over the last 28 local days, average intake of the days counted as in Trends minus the weight trend change x 7700 kcal/kg over the days between the two trend values, rounded to 10 kcal, with `low`/`medium`/`high` confidence. No estimate (and a `reason`) below 14 counted days or weigh-ins on fewer than 6 days spanning fewer than 14. Using the user's own log keeps a steady estimation bias on both sides. The plain-language method is in the module docstring.
- Meals stop arriving (`services/integration.py:arrival_status`, field `arrival_status` of `/api/v1/integration-status`): judged from when meals reached the server (`created_at`), not when they were eaten. `unknown` below 23 save days in the last 28 local days; `quiet` when nothing was saved yesterday, or today after the usual first-save time (80th percentile) plus 2 h; otherwise `ok`. It describes the integration, never the user's eating or logging, and its wording must stay neutral.
- Nutrient reference data must come from open sources with compatible licenses (e.g. USDA FoodData Central: public domain; Open Food Facts: ODbL, so attribute and keep it as a separate database, not mixed into our code).

## MCP tool guidelines

- Tool names: `snake_case` verbs (`log_meal`, `get_daily_summary`, `search_foods`).
- Inputs and outputs are Pydantic models with field descriptions. The LLM reads them, so be explicit about units.
- Tools must be idempotent where possible (accept a client-generated id for writes).
- Return concise structured results, not prose.
- Annotate every tool honestly: `readOnlyHint`, `destructiveHint` for writes (true only when it removes data, like `delete_meal`) and `openWorldHint: false` (tools only touch the user's own server). The MCP defaults say destructive and open-world, which makes clients warn more. ChatGPT still asks the user to confirm every tool that is not read-only, once per conversation.
- Use the server `instructions` to tell the client how to break a meal into items before logging, and to mention the iOS app.
- Never break the user's existing workflow. Users may already track food elsewhere (a ChatGPT project with their own calorie instructions, a Google Sheets log via another connector). The assistant keeps doing all of that unchanged (same answer format, same calculation, same other tools); HealthyEating is added on top.
- Still collect complete data: every meal needs calories, protein, fat and carbohydrates (fiber, sugar and sodium are optional: passed when known or estimable, never asked for). Numbers from the user's workflow (`source="from_conversation"`) are stored unchanged; the assistant estimates the missing ones and declares them in `estimated_fields`. A meal with all four values is always saved, even with `open_questions`: the questions go to the user after the normal answer, and the answer updates the same meal (`replaces_meal_id`; a complete correction asks nothing more). `needs_more_info` (nothing saved) is only for values that cannot be estimated at all. Never lose a meal because a question went unanswered.
- Targets from the chat (`set_targets`, `services/settings.py:set_targets_from_chat`) are stricter than the app: a target needs a bound and is never a single number (the assistant asks for a range), and in Calm mode the calorie target is neither shown (`visible_targets`, also used by `get_profile`) nor changed, like on the app's targets screen.
- Weight tools (`mcp_server/tools/weights.py`): `log_weight` only when the user gives a weight (never ask for it), in kg or lb (stored in kg), with `replaces_weight_id` to correct; the first weigh-in turns weight tracking on and its `user_message` says how to turn it off in the app. `list_weights` returns nothing while tracking is off. Never comment on a weight change.
- Write tools report whether the data reached HealthyEating (`saved_to_healthy_eating` + `user_message`), so users can tell when the integration is not working.
- After changing tool descriptions, parameters, results or the server `INSTRUCTIONS`, run the evaluation suite (`uv run python -m evals`) and report its result. Add a scenario in `evals/scenarios.py` for new workflow rules.

## Code conventions

- Fully typed, async I/O only (no blocking calls in request paths).
- Pydantic v2 models for all external input/output; SQLAlchemy models are not returned directly.
- Every `/api/v1` route has an explicit camelCase `operation_id`, a response model and its error statuses documented with `error_response()`; errors are raised as `HTTPException` and reach the client as `{code, message, details}`. Additions are fine; anything that breaks the contract needs `/api/v2`. Regenerate `openapi.json` and commit it with the change.
- Every DB query in a request is scoped to the authenticated user. Add a test for cross-user access when adding an endpoint or tool.
- Schema changes only via Alembic migrations, reviewed by hand after autogenerate.
- Tests: unit tests for `nutrition/` and `services/`; integration tests against a real Postgres (docker) for DB, auth and MCP flows. Mock upstream OIDC providers.
- Commit messages: imperative mood, short subject line.

## Do not

- Add dependencies with non-MIT-compatible licenses or that require a hosted service.
- Return or log upstream provider tokens.
- Put secrets in code, tests or fixtures (use `.env`, which is git-ignored).
- Break the REST API used by iOS without versioning (`/api/v1` → `/api/v2`).
