# AGENTS.md: healthy-eating-backend

Guidance for AI coding agents and contributors working in this repository.

## Project overview

Healthy Eating is an open-source (MIT), self-hostable app for tracking nutrition. It has two parts:

- **healthy-eating-backend** (this repo): a Python service that exposes an **MCP server** with tools for logging food and computing calories, macronutrients, vitamins and minerals. It also provides a **REST API** for the iOS app and an **OAuth 2.1 authorization server** with social login. Data is stored in **PostgreSQL**.
- **healthy-eating-ios** (separate repo): a SwiftUI app that shows statistics and progress.

The LLM in the user's MCP client (Claude, ChatGPT, etc.) recognizes the food and estimates portions. The backend does **not** call an LLM. Its tools accept structured input (food items + amounts), look up nutrient data, compute totals and persist them.

Status: early v1. Implemented: OAuth 2.1 server with social and dev login, MCP endpoint with `get_profile`, `set_timezone`, `set_targets`, `log_meal` (meals stored in the `meals` table), `set_day_status`, `list_meals`, `get_daily_summary`, `delete_meal`, `list_usual_meals`, `relog_meal`, `find_meals`, `import_meals`, `find_foods`, `log_weight`, `list_weights` and `delete_weight` tools, REST `/api/v1/me`, `/api/v1/me/settings`, `/api/v1/me/logins` (list, add another social login, remove one), `/api/v1/meals` (list, get, correct, delete, re-log), `/api/v1/usual-meals`, `/api/v1/days` (daily summaries), `/api/v1/days/{date}/status`, `/api/v1/trends`, `/api/v1/weekly-review`, `/api/v1/weights` (list with trend, add one or a batch, correct, delete, delete all, delete Apple Health samples by id), `/api/v1/expenditure`, `/api/v1/integration-status`, `/api/v1/connections`, and the widget token (`POST /api/v1/widget-token`, `GET /api/v1/widget/today`), data export (`GET /api/v1/export`) and import of an export (`POST /api/v1/import`), Docker dev stack, tests. Production setup: `compose.production.yml` and `docs/self-hosting.md`. Keep this file in sync when the layout or commands change.

## Hard constraints

1. **Open source only.** Every dependency must use an MIT-compatible license (MIT, BSD, Apache-2.0, ISC, PSF). No GPL/AGPL/SSPL, no proprietary SDKs, no required SaaS. Check the license before adding a dependency.
2. **Self-hostable with minimal moving parts.** A deployment is this app + PostgreSQL (+ optional reverse proxy). Do not add services such as Redis, Keycloak or message queues without an explicit decision.
3. **All configuration via environment variables** (documented in `.env.example`). Features with external credentials, like each social provider, are enabled only when their variables are set.
4. **Health data is sensitive** (likely GDPR Art. 9). Never log meal contents, nutrient values, tokens or emails. Support full account export and deletion: `GET /api/v1/export` (format in `docs/export-format.md`; add new user data to it, and to the import in `services/data_import.py` when it is part of the history) and `DELETE /api/v1/me` relies on `ON DELETE CASCADE` from every user-owned table to `users`: give each new user-owned table that foreign key (`tests/test_delete_account.py` checks every table with a `user_id` column).

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
    routes.py        # /login, /login/cancel, /link, /link/cancel, /auth/start/{provider}, /auth/callback/{provider}, /auth/dev-login
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
  nutrition/         # shared nutrition types; micronutrients.py (vitamins and minerals, units, USDA ids);
                     # usda_build.py builds data/usda_reference.csv.gz (+ .json) from the USDA downloads
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

# Rebuild the bundled USDA reference data (CSV zips of FNDDS, SR Legacy and Foundation Foods from
# https://fdc.nal.usda.gov/download-datasets/ in a folder outside the repo); commit both outputs
uv run python -m healthy_eating.nutrition.usda_build <folder>

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
- Adding a login (`services/linking.py`, `link_requests`, healthy-eating-backend#20) works like an authorization code with PKCE: the app posts a PKCE challenge to `POST /api/v1/me/logins/link` and opens the returned `/link?request=...` page; the user signs in with the provider there (the browser session carries `link_request` instead of `login_request`, and `_finish_login` branches on it); the browser goes to `healthyeating://link/callback?code=...` (or `?error=cancelled|expired`), and the app finishes with `POST /api/v1/me/logins/link/complete` (code + verifier, same user). A forwarded link URL cannot be finished by anyone else. Each code works once, also when refused (the router commits before raising). A login that belongs to another account is refused with 409, never merged. `DELETE /api/v1/me/logins/{id}` keeps at least one login (409).
- Apple: client secret is an ES256 JWT generated on the fly; callback is `form_post` (POST); the user's name is only sent on first login, so persist it then.
- Google and Microsoft are asked for `prompt=select_account` on every sign-in and link, so the account picker always shows (the browser may be signed in to another account, and linking a second account of the same provider needs the choice).
- Microsoft: use the `common` endpoint; validate the issuer against the `https://login.microsoftonline.com/{tenantid}/v2.0` template.
- Strict redirect URI validation: `https`, loopback (`http://127.0.0.1` / `localhost`) and the app's custom scheme only.
- Auth codes are short-lived and single-use. All tokens are opaque and stored only as SHA-256 hashes. Every sign-in is a row in `grants`; its tokens share the `grant_id`. Refresh tokens rotate, and presenting a rotated refresh token again revokes the whole grant. Revoke grants only through `services/connections.py:revoke_grant`, so the grant row and all tokens end together.
- Expired sign-in data is deleted inside the app process (`services/cleanup.py`, an asyncio task started in the lifespan, every `CLEANUP_INTERVAL_SECONDS`, default 1 h): login and link requests, authorization codes, access and refresh tokens past `expires_at`, then grants with no token left. Rotated or revoked refresh tokens stay until they expire, so reuse detection keeps working. OAuth clients are never deleted (meals refer to them).
- Expose `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`. The MCP endpoint and the REST API accept the same bearer tokens.
- Scopes: `nutrition` (every token carries it; the MCP endpoint requires it) and `offline_access` (advertised and accepted because some clients, ChatGPT, only use refresh tokens when it is listed; refresh tokens are issued either way). A sign-in always includes `nutrition`, even when the client asked only for `offline_access`. Clients registered before `offline_access` existed can only request `nutrition`; they register again to get it.
- Widget tokens (`services/widget.py`, `widget_tokens`): the iOS widgets read `GET /api/v1/widget/today` with their own read-only token, so they never share the app's refresh token (two clients rotating one refresh token would trigger reuse detection and revoke the sign-in). `POST /api/v1/widget-token` (the app's access token) issues one per grant and replaces the earlier one; it is stored as a SHA-256 hash, works only on that endpoint (the endpoint takes no other token), and ends with its grant (revoked grants are refused, deleted grants and accounts cascade).
- `DEV_LOGIN_ENABLED` adds an email-only login for local development and tests. `Settings` refuses it in production; never weaken that check.
- Library behavior to keep in mind: FastMCP answers `invalid_grant` with HTTP 401 (MCP spec), the MCP SDK's `/revoke` requires a `client_secret` form field even for public clients (send it empty), and the issuer (`BASE_URL`) must be `https` or `localhost`.

## Domain rules

- Store nutrients in canonical units: energy in `kcal`, macros in `g`, minerals in `mg`, trace elements and most vitamins in `µg`. Convert at the edges only.
- Fiber (`fiber_g`), sugar (`sugar_g`) and sodium (`sodium_mg`) are optional on every meal (NULL when unknown; `OPTIONAL_NUTRIENT_FIELDS`); the required set stays calories and three macros (`NUTRIENT_FIELDS`). They follow the same provenance rule and may appear in `estimated_fields` (`TRACKED_NUTRIENT_FIELDS`). Sugar above carbohydrates is a warning, never an error. Day totals report each as `{amount, meal_count}` and Trends as `{average, days}` (only meals and counted days that have a value), so a partial value never poses as a full one. The weekly review's estimated share counts the values each meal has.
- A logged food entry stores a **snapshot** of its computed nutrient values, so later changes to reference data never rewrite history.
- Corrections (`replaces_meal_id`) update the existing `meals` row in place, so a meal is never counted twice. `eaten_at` is stored in UTC; `utc_offset_minutes` keeps the offset of the meal's local time: the one the user gave, otherwise the one of the user's time zone (`users.timezone`) at that moment, or NULL when neither is known. A time sent without an offset is read in the user's time zone, or stored as UTC with the offset unknown when no time zone is set.
- Corrections from the app (`PATCH /api/v1/meals/{id}`) update the same row through `services/meals.py:edit_meal`. A nutrition value the user changes is no longer estimated; a value sent unchanged keeps its mark. Input limits (`MAX_*` in `services/meals.py`) are shared by `log_meal` and the REST API.
- Usual meals (`services/usual_meals.py`, `/api/v1/usual-meals`, `POST /api/v1/meals/{id}/relog`): a dish is usual when its name (case and spacing ignored) was eaten at least twice in the last 90 days; its latest meal is the template. Re-logging copies the template's name, description, portion, values (optional nutrients too), `source`, `source_note`, `estimated_fields` and `confidence` (decision healthy-eating-ios#13); only `eaten_at` is new and the meal belongs to the client that asked. A client-chosen id (`id` in REST, `request_id` in MCP) makes a repeated request return the same meal. Only existing meals can be re-logged; new food is logged in the chat. Imported meals do not count as usual meals. Re-logging with another `portion_grams` (MCP `relog_meal`, owner decision 2026-10-27) scales every value by the ratio of the weights (macros, optional nutrients, item grams, label values of weighed items, micronutrients) and keeps the provenance; it needs the template's portion weight (`PortionUnknownError` otherwise), and label items without grams keep their values.
- Earlier meals with known values (`services/meal_search.py`, MCP `find_meals`, owner decision 2026-10-27): so a product the user had from a label is not estimated anew at another weight. Only meals with `source = from_conversation`, none of the four main values estimated and a known portion weight are found (never the assistant's estimates), in the last 365 days, by the words of the dish and item names (case ignored, any script, prefix match), one result per dish name (the latest), with values per 100 g. `log_meal` adds `earlier_meal` and a question to `questions_for_user` when a new, estimated meal with a weight has the name of an earlier known meal whose values per 100 g clearly differ (calories by more than 10 % and 5 kcal, or a macro by more than 20 % and 1 g); never for corrections or for meals with known values of their own.
- Importing history (`services/meal_import.py`, MCP `import_meals`, healthy-eating-backend#27): the assistant sends the user's earlier tracking (a ChatGPT project, a spreadsheet, another app) in parts of up to 100 entries, each following the `log_meal` rules (all four values, the history's numbers unchanged as `from_conversation`, filled-in values in `estimated_fields`; a day total is one entry named "Day total"). Imported meals keep `meals.imported_from` (also `source_note` when none was given, the REST meal and the export). Nothing is counted twice: an entry with the same name (case and spacing ignored), minute and calories as a stored meal is skipped, and a local day that already has meals logged as they were eaten (not imported) is skipped whole. Entries without all values or in the future are rejected with their index and reason; the rest are imported. Days that get imported meals are marked complete (`mark_imported_days`).
- Importing an export (`services/data_import.py`, `POST /api/v1/import`, owner decision 2026-10-26: only HealthyEating's own export, JSON or the CSV ZIP, recognized by content, at most 50 MB; preview with `?dry_run=true`, run in a savepoint that is rolled back): moves a history between accounts or servers. Imports meals (all fields, items; micronutrients computed again), weigh-ins (with the Apple Health `external_id`; they turn weight tracking on) and the marks of days that get meals; never the account, logins, settings or connections. Meals keep `imported_from` as exported (empty for live-logged meals, so they stay usual meals) and keep their ids and `created_at` unless another account uses the id. Duplicates by id, by the meal key above, or for weigh-ins by sample id or time and weight are skipped. Invalid entries are reported (`rejected`, first 50) and left out. Both imports pass one `SearchCache` (`services/food_matching.py`) to `set_items`, so each item name is searched once per import (3,000 meals with items: about 60 s without it, under 2 s with it; reverse proxies often stop requests after 60 s). The rules are in the module docstring and `docs/export-format.md`.
- Day completeness (`services/days.py`): a local day can be marked `complete` or `partial` by the user (app) or the assistant (`set_day_status`). Averages count only complete days with meals once the user has marked any day complete; otherwise every day with meals except partial ones. Days without meals never count. Use `day_counts()` for every average. Imports mark the days they add (`mark_imported_days`, never over an existing mark) with `day_statuses.from_import`: such a mark makes the day count, but `counts_complete_days_only` ignores it, so an import never switches the user's counting rule; the user's own mark replaces it.
- A meal's local day uses its stored offset first, then the user's current time zone, then UTC (`services/local_time.py`). Use these helpers for every per-day query so the REST API and MCP tools agree.
- Store timestamps in UTC plus the user's IANA timezone. Daily aggregates are computed in the user's local day.
- Weekly review (`services/weekly_review.py`, `/api/v1/weekly-review?week=`): a local week runs Monday to Sunday. Averages count days like Trends (today only once complete, never later days) and name every left-out day with its reason; weekdays and weekend use the same rule. Per-meal facts (protein per meal and its 10 g buckets, late meals from 21:00 to 04:00 local time, share of estimated values, low-confidence meals) use every meal of the week. Neutral facts only, no scores.
- Weight (`services/weights.py`, `/api/v1/weights`) is opt-in (decision healthy-eating-ios#14): `users.weight_tracking` is off until the first weigh-in from the chat or the app, or until the user turns it on; Apple Health entries are refused (409) while it is off; turning it off keeps the entries, deleting them is separate (`DELETE /api/v1/weights` with `{"confirm": "delete-all-weights"}`). Stored in kg (`weight_unit` is display only), with `measured_at` in UTC plus offset like meals; Apple Health samples are de-duplicated by `external_id`, and a sample deleted in Apple Health is deleted here through `POST /api/v1/weights/apple-health-deletions` (only `apple_health` entries match; unknown ids are ignored). The iOS app syncs both ways: it also writes chat and app weigh-ins to Apple Health, so their ids stay ours, not `external_id`s. Trend: daily mean, then a gap-aware 10 % exponential average from the first weigh-in; weekly change over the last 28 days (at least 14). The method is in the module docstring; keep wording about weight neutral.
- Energy expenditure (`services/expenditure.py`, `/api/v1/expenditure`): over the last 28 local days, average intake of the days counted as in Trends minus the weight trend change x 7700 kcal/kg over the days between the two trend values, rounded to 10 kcal, with `low`/`medium`/`high` confidence. No estimate (and a `reason`) below 14 counted days or weigh-ins on fewer than 6 days spanning fewer than 14. Using the user's own log keeps a steady estimation bias on both sides. The plain-language method is in the module docstring.
- Meals stop arriving (`services/integration.py:arrival_status`, field `arrival_status` of `/api/v1/integration-status`): judged from when meals reached the server (`created_at`), not when they were eaten. `unknown` below 23 save days in the last 28 local days; `quiet` when nothing was saved yesterday, or today after the usual first-save time (80th percentile) plus 2 h; otherwise `ok`. It describes the integration, never the user's eating or logging, and its wording must stay neutral.
- Vitamins and minerals (healthy-eating-backend#25, owner decision 2026-10-26) come from open reference data for the meal's food items, never from the assistant: USDA FoodData Central (CC0), bundled as `nutrition/data/usda_reference.csv.gz` (13,694 foods; per 100 g: energy and the 18 nutrients of `MICRONUTRIENTS` in mg or µg) and loaded at startup into `reference_foods` / `reference_data` (`services/reference_data.py`: only when the bundled version changed, in one transaction under an advisory lock). This is public reference data, not a curated food database of our own (non-goal); it is never edited by hand, only rebuilt from USDA downloads. Matching (`services/food_matching.py`): Postgres full-text search on the item name with the ranking in the module docstring; only a `matched` item (every food word found) gets values, `weak` and `none` get none; an item may name a food by `food_id`. Values scale linearly with grams; a nutrient a food has no value for stays unknown, never zero, and totals count the items that have a value. Tests keep `reference_*` tables between tests (the app loads them once).
- Meal items (`meal_items`, `meals.micronutrients`, `services/meals.py:set_items`): `log_meal` takes optional `items` (name, grams, optional `food_id`, at most 30). They never change the meal's calories or macros. The meal stores each item with its status and, when matched, a snapshot of the USDA food (id and description; no foreign key, since reference data is replaced on updates), and `micronutrients` (totals of the matched items; NULL without items, `{}` when none matched). A correction with items replaces them, without items keeps them; re-logging copies them; imported meals have none. The REST meal and the export show `micronutrients` and `items`. Supplements and packaged foods (owner decision 2026-10-26): an item may carry `label_values` (vitamins and minerals printed on the label or stated by the user, for the amount taken, in canonical units; the assistant converts IU only where exact, e.g. vitamin D 40 IU = 1 µg, and never estimates them). Such an item is not matched, has the status `label`, may have no grams (a capsule; a CHECK allows NULL grams only for `label`), and counts in the meal's totals. REST keeps `items` for reference-food items (always with grams) and lists label items separately in `label_items` (`name`, `grams`, `values`), so the contract stays additive. New meals start with a loaded empty `items` list: matching autoflushes, and replacing an unloaded collection would need a lazy load, which async sessions forbid.
- Micronutrient totals (`services/summaries.py`, `services/trends.py`): a day's `micronutrients` is, per nutrient, `{amount, meal_count}` over the meals that have a value (2 decimals: B vitamins are fractions of a mg), like fiber. Trends' `micronutrient_averages` (`{average, days}`) differ from fiber on purpose: a counted day is used only when every meal of it has a value, so a day where only some meals have items (or meals from before #25) does not pull the average down. The weekly review's `micronutrient_averages` use the same helper (`services/trends.py:micronutrient_averages`). MCP `get_daily_summary` gives each day's `micronutrients` (`{amount, meal_count}`) and `list_meals` each meal's `micronutrients`; `import_meals` entries may carry `items` like `log_meal`.
- Reference group (`users.reference_group`, `REFERENCE_INTAKES` in `nutrition/micronutrients.py`): optional, off (NULL) by default; a life stage the user picks (healthy-eating-backend#34, owner decision 2026-10-27; no birth date or age is stored, adults only): `women_` or `men_` with `19_30`, `31_50`, `51_70` or `71_plus`, and `pregnancy_` or `breastfeeding_` with `19_30` or `31_50`. `GET/PATCH /api/v1/me/settings` carry `reference_group` (null turns it off) and `micronutrient_references` (the group's `{min, max}` per nutrient, empty when off). PATCH still accepts the names before #34, `adult_women` and `adult_men`, and stores `women_31_50` and `men_31_50` (the values those groups had); the migration mapped stored values the same way. Values: US Dietary Reference Intakes for each life stage via the NIH Office of Dietary Supplements fact sheets, one column per group in `_MINIMUMS` and `_MAXIMUMS` (`tests/test_reference_group.py` checks values against the fact sheets); `min` is the RDA or AI, `max` the upper limit only where it applies to food (none for vitamin E, niacin, folate, magnesium and vitamin A, whose limits concern supplements or synthetic forms). Reference values, never targets: no wording about being over or under them.
- Vitamin and mineral goals (`users.micronutrient_goals`, JSONB `{code: {min, max}}`, healthy-eating-backend#35, owner decision 2026-10-27): the user's own daily range for a nutrient, set in the app (`PATCH /api/v1/me/settings` `micronutrient_goals`; only the codes listed change, `null` removes one) or the chat (`set_targets` `vitamins_and_minerals`). A goal replaces that nutrient's life stage reference and applies without a life stage too; `services/settings.py:micronutrient_ranges` gives the range that applies with its `source` (`personal` or `reference`), in the settings response as `micronutrient_ranges` and in `get_profile` as `vitamin_and_mineral_ranges`. Same rules as targets: ranges only, either bound open, never negative; part of the export (`settings.micronutrient_goals`, CSV `goal_<code>_<min|max>`), not of data import (settings are never imported).
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
- Targets from the chat (`set_targets`, `services/settings.py:set_targets_from_chat`) are stricter than the app: a target needs a bound and is never a single number (the assistant asks for a range), and in Calm mode the calorie target is neither shown (`visible_targets`, also used by `get_profile`) nor changed, like on the app's targets screen. Vitamin and mineral goals go through the same tool and rules (bounds compared to 0.01, Calm mode does not affect them); a daily amount the user should get ("my doctor said 50 µg vitamin D a day") is a lower bound, so the assistant saves it as `min` instead of asking (eval `vitamin_goal`). Its `user_message` says "goals" when only vitamins and minerals changed.
- `import_meals` (`mcp_server/tools/importing.py`) only when the user asks to import, move or copy earlier history into HealthyEating; a meal eaten now always goes through `log_meal`. Its `user_message` names how many meals were imported and for which dates (neutral, no judgment of the history).
- `log_meal` asks for `items` (each food plainly named, with grams) for vitamins and minerals, and never to mention them unless the user asks; its result lists each item's match. `find_foods` (`mcp_server/tools/foods.py`, read-only) finds a `food_id` to correct a wrong or missing match.
- Usual meal tools (`mcp_server/tools/usual.py`): `relog_meal` when the user says they ate exactly the same as before ("same breakfast as yesterday"), with the `meal_id` from `list_usual_meals`, `find_meals` or `list_meals`, and `portion_grams` when only the weight differs; anything else that differs goes through `log_meal`. Before estimating a packaged or branded product, or a dish the user may have logged before, the assistant calls `find_meals` (server `INSTRUCTIONS`, `log_meal` description; eval `known_product`).
- Weight tools (`mcp_server/tools/weights.py`): `log_weight` whenever the user states a weight or asks to update or record it, in any language, also when the assistant keeps it in its own memory (never ask for it), in kg or lb (stored in kg), with `replaces_weight_id` to correct; the first weigh-in turns weight tracking on and its `user_message` says how to turn it off in the app. `list_weights` returns nothing while tracking is off. Never comment on a weight change.
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
