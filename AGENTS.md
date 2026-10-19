# AGENTS.md: healthy-eating-backend

Guidance for AI coding agents and contributors working in this repository.

## Project overview

Healthy Eating is an open-source (MIT), self-hostable app for tracking nutrition. It has two parts:

- **healthy-eating-backend** (this repo): a Python service that exposes an **MCP server** with tools for logging food and computing calories, macronutrients, vitamins and minerals. It also provides a **REST API** for the iOS app and an **OAuth 2.1 authorization server** with social login. Data is stored in **PostgreSQL**.
- **healthy-eating-ios** (separate repo): a SwiftUI app that shows statistics and progress.

The LLM in the user's MCP client (Claude, ChatGPT, etc.) recognizes the food and estimates portions. The backend does **not** call an LLM. Its tools accept structured input (food items + amounts), look up nutrient data, compute totals and persist them.

Status: early v1. Implemented: OAuth 2.1 server with social and dev login, MCP endpoint with `get_profile`, `set_timezone` and `log_meal` (meals stored in the `meals` table) tools, REST `/api/v1/me`, `/api/v1/me/settings` and `/api/v1/meals` (list, get, delete), Docker dev stack, tests. Statistics (daily summary, trends) are next. Keep this file in sync when the layout or commands change.

## Hard constraints

1. **Open source only.** Every dependency must use an MIT-compatible license (MIT, BSD, Apache-2.0, ISC, PSF). No GPL/AGPL/SSPL, no proprietary SDKs, no required SaaS. Check the license before adding a dependency.
2. **Self-hostable with minimal moving parts.** A deployment is this app + PostgreSQL (+ optional reverse proxy). Do not add services such as Redis, Keycloak or message queues without an explicit decision.
3. **All configuration via environment variables** (documented in `.env.example`). Features with external credentials, like each social provider, are enabled only when their variables are set.
4. **Health data is sensitive** (likely GDPR Art. 9). Never log meal contents, nutrient values, tokens or emails. Support full account export and deletion.

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
    tools/           # one module per tool group, each exposing register(mcp, db)
  api/
    deps.py          # SessionDep, CurrentUserId (bearer token auth)
    health.py        # /health
    v1/              # REST routers for the iOS app
  services/          # business logic shared by MCP tools and REST
  nutrition/         # shared nutrition types; later reference data, unit conversion
migrations/          # Alembic (async), dated revision files
docker/postgres/init # creates the test database on first start
tests/               # pytest, real Postgres; tests/oauth_helpers.py drives the OAuth flow
evals/               # MCP evaluation suite: real model via opencode, run on demand (evals/README.md)
docker-compose.yml   # local development stack
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
- Auth codes are short-lived and single-use. All tokens are opaque and stored only as SHA-256 hashes. Tokens issued together share a `grant_id`; refresh tokens rotate, and presenting a rotated refresh token again revokes the whole grant.
- Expose `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`. The MCP endpoint and the REST API accept the same bearer tokens.
- `DEV_LOGIN_ENABLED` adds an email-only login for local development and tests. `Settings` refuses it in production; never weaken that check.
- Library behavior to keep in mind: FastMCP answers `invalid_grant` with HTTP 401 (MCP spec), the MCP SDK's `/revoke` requires a `client_secret` form field even for public clients (send it empty), and the issuer (`BASE_URL`) must be `https` or `localhost`.

## Domain rules

- Store nutrients in canonical units: energy in `kcal`, macros in `g`, minerals in `mg`, trace elements and most vitamins in `µg`. Convert at the edges only.
- A logged food entry stores a **snapshot** of its computed nutrient values, so later changes to reference data never rewrite history.
- Corrections (`replaces_meal_id`) update the existing `meals` row in place, so a meal is never counted twice. `eaten_at` is stored in UTC; `utc_offset_minutes` keeps the offset of the meal's local time: the one the user gave, otherwise the one of the user's time zone (`users.timezone`) at that moment, or NULL when neither is known. A time sent without an offset is read in the user's time zone, or stored as UTC with the offset unknown when no time zone is set.
- A meal's local day uses its stored offset first, then the user's current time zone, then UTC (`services/local_time.py`). Use these helpers for every per-day query so the REST API and MCP tools agree.
- Store timestamps in UTC plus the user's IANA timezone. Daily aggregates are computed in the user's local day.
- Nutrient reference data must come from open sources with compatible licenses (e.g. USDA FoodData Central: public domain; Open Food Facts: ODbL, so attribute and keep it as a separate database, not mixed into our code).

## MCP tool guidelines

- Tool names: `snake_case` verbs (`log_meal`, `get_daily_summary`, `search_foods`).
- Inputs and outputs are Pydantic models with field descriptions. The LLM reads them, so be explicit about units.
- Tools must be idempotent where possible (accept a client-generated id for writes).
- Return concise structured results, not prose.
- Use the server `instructions` to tell the client how to break a meal into items before logging, and to mention the iOS app.
- Never break the user's existing workflow. Users may already track food elsewhere (a ChatGPT project with their own calorie instructions, a Google Sheets log via another connector). The assistant keeps doing all of that unchanged (same answer format, same calculation, same other tools); HealthyEating is added on top.
- Still collect complete data: every meal needs calories, protein, fat and carbohydrates. Numbers from the user's workflow (`source="from_conversation"`) are stored unchanged; the assistant estimates the missing ones and declares them in `estimated_fields`. A meal with all four values is always saved, even with `open_questions`: the questions go to the user after the normal answer, and the answer updates the same meal (`replaces_meal_id`; a complete correction asks nothing more). `needs_more_info` (nothing saved) is only for values that cannot be estimated at all. Never lose a meal because a question went unanswered.
- Write tools report whether the data reached HealthyEating (`saved_to_healthy_eating` + `user_message`), so users can tell when the integration is not working.
- After changing tool descriptions, parameters, results or the server `INSTRUCTIONS`, run the evaluation suite (`uv run python -m evals`) and report its result. Add a scenario in `evals/scenarios.py` for new workflow rules.

## Code conventions

- Fully typed, async I/O only (no blocking calls in request paths).
- Pydantic v2 models for all external input/output; SQLAlchemy models are not returned directly.
- Every DB query in a request is scoped to the authenticated user. Add a test for cross-user access when adding an endpoint or tool.
- Schema changes only via Alembic migrations, reviewed by hand after autogenerate.
- Tests: unit tests for `nutrition/` and `services/`; integration tests against a real Postgres (docker) for DB, auth and MCP flows. Mock upstream OIDC providers.
- Commit messages: imperative mood, short subject line.

## Do not

- Add dependencies with non-MIT-compatible licenses or that require a hosted service.
- Return or log upstream provider tokens.
- Put secrets in code, tests or fixtures (use `.env`, which is git-ignored).
- Break the REST API used by iOS without versioning (`/api/v1` → `/api/v2`).
