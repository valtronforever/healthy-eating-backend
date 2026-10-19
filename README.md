# Healthy Eating: backend

Self-hostable backend for **Healthy Eating**, an open-source nutrition tracker: the private, honest dashboard for people who log food by talking to their AI assistant. You keep using ChatGPT, Claude or any MCP client the way you already do; the assistant saves each meal to your own server, marking which values it estimated, and the iOS app turns that into weekly trends.

- **MCP server** (`/mcp`, Streamable HTTP) for logging food and reading nutrition data from LLM clients such as Claude or ChatGPT.
- **OAuth 2.1 authorization server** with Dynamic Client Registration and PKCE. Users sign in with Google, Microsoft or Apple; the first sign-in creates the account.
- **REST API** (`/api/v1`) for the [iOS app](https://github.com/valtronforever/healthy-eating-ios).
- **PostgreSQL** storage. A deployment is just this app plus Postgres.

Status: early development. Authentication, the MCP endpoint (tools `get_profile`, `set_timezone`, `log_meal` and `set_day_status`) and the REST API for the account, settings (`/api/v1/me/settings`) and meals (`/api/v1/meals`: list by local day, get, correct, delete) and day completeness (`/api/v1/days/{date}/status`) work; statistics and the remaining nutrition features are in progress.

## Quick start (Docker)

Requirements: Docker with Compose v2.

```bash
git clone https://github.com/valtronforever/healthy-eating-backend.git
cd healthy-eating-backend
cp .env.example .env
docker compose up --build
```

This starts Postgres and the app with auto-reload (source code is mounted into the container). Migrations run automatically on start.

| URL | What |
|---|---|
| http://localhost:8000/health | Health check |
| http://localhost:8000/docs | REST API docs (OpenAPI) |
| http://localhost:8000/mcp | MCP endpoint |
| http://localhost:8000/.well-known/oauth-authorization-server | OAuth discovery |

If ports 8000 or 5432 are taken, set `APP_PORT` / `POSTGRES_PORT` in `.env` (when changing `APP_PORT`, update `BASE_URL` to match).

### Connect an MCP client

Point any MCP client that supports remote servers with OAuth at `http://localhost:8000/mcp`. For example:

```bash
# Claude Code
claude mcp add --transport http healthy-eating http://localhost:8000/mcp

# MCP Inspector (choose "Streamable HTTP" and enter the URL above)
npx @modelcontextprotocol/inspector
```

The client registers itself and opens the login page in your browser. With `DEV_LOGIN_ENABLED=true` (the default in `.env.example`) you can sign in with any email address, no social provider needed. Then call the `get_profile` tool.

## Development on the host

Requirements: [uv](https://docs.astral.sh/uv/) and Docker (for Postgres).

```bash
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn --factory healthy_eating.main:create_app --reload
```

### Tests and checks

Tests run against a real Postgres database (`healthy_eating_test`, created automatically by the Compose setup).

```bash
uv run pytest                     # or: docker compose exec app pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests evals
```

### MCP evaluation suite

[`evals/`](evals/README.md) checks how a real model uses the MCP tools: it starts the app, connects a real [opencode](https://opencode.ai) to it and runs chat scenarios (meal with the user's own numbers, photo, existing calorie workflow with a Sheets log, unclear portion, correction, meal eaten earlier, user mentions their time zone, user is done for the day, server failure, non-meal question), then checks the database, the tool calls and the replies. Run it after changing tool descriptions, server instructions or tool results. It needs opencode signed in to a model provider and uses its own database (`healthy_eating_eval`).

```bash
uv run python -m evals            # all scenarios, 3 runs each (about two minutes)
uv run python -m evals --list
```

### Database migrations

```bash
uv run alembic revision --autogenerate -m "add meals"   # after changing models; review the file
uv run alembic upgrade head
```

If you created the Postgres volume before the test database init script existed, create it once with:

```bash
docker compose exec postgres createdb -U healthy healthy_eating_test
```

To start over with an empty database: `docker compose down -v`.

## Configuration

All settings are environment variables (or entries in `.env`). See [`.env.example`](.env.example) for the full list.

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` enables strict checks |
| `BASE_URL` | `http://localhost:8000` | Public URL as seen by clients. Must be `https` in production |
| `DATABASE_URL` | local Postgres | `postgresql+asyncpg://user:pass@host:5432/db` |
| `SECRET_KEY` | dev-only value | Required in production (at least 32 characters) |
| `DEV_LOGIN_ENABLED` | `false` | Email-only login for development. Rejected in production |
| `GOOGLE_*`, `MICROSOFT_*`, `APPLE_*` | unset | A provider appears on the login page only when all its variables are set |

## Social login setup

Register an OAuth application with each provider you want to offer. The redirect (callback) URI is always:

```
{BASE_URL}/auth/callback/{google|microsoft|apple}
```

### Google

1. Open [Google Cloud Console → APIs & Services](https://console.cloud.google.com/apis/credentials) and configure the OAuth consent screen.
2. Create credentials → **OAuth client ID** → application type **Web application**.
3. Add the authorized redirect URI, e.g. `http://localhost:8000/auth/callback/google`.
4. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### Microsoft

1. Open [Microsoft Entra admin center → App registrations](https://entra.microsoft.com/) → **New registration**.
2. Supported account types: **Accounts in any organizational directory and personal Microsoft accounts**.
3. Redirect URI: platform **Web**, e.g. `http://localhost:8000/auth/callback/microsoft`.
4. Under **Certificates & secrets**, create a client secret.
5. Optional: under **Token configuration**, add the `email` optional claim to the ID token.
6. Set `MICROSOFT_CLIENT_ID` (the Application ID) and `MICROSOFT_CLIENT_SECRET` (the secret's value).

### Apple

Requires a paid Apple Developer account, and Apple only accepts `https` redirect URIs on a public domain (not `localhost`). For local testing, use an HTTPS tunnel and set `BASE_URL` to the tunnel URL.

1. In [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/identifiers/list), create an **App ID** with *Sign in with Apple* enabled.
2. Create a **Services ID**; enable *Sign in with Apple*, add your domain and the return URL `https://<your-host>/auth/callback/apple`. Its identifier is `APPLE_SERVICES_ID`.
3. Create a **Key** with *Sign in with Apple* enabled and download the `.p8` file. Its Key ID is `APPLE_KEY_ID`.
4. Set `APPLE_TEAM_ID` (shown in your membership details) and `APPLE_PRIVATE_KEY` (the `.p8` contents; newlines may be written as `\n`).

## Production

The `prod` target of the [Dockerfile](Dockerfile) builds a slim image that runs as a non-root user and applies migrations on start:

```bash
docker build --target prod -t healthy-eating-backend .
```

Run it behind a TLS-terminating reverse proxy with at least `ENVIRONMENT=production`, `BASE_URL=https://…`, `SECRET_KEY` and `DATABASE_URL` set. A complete self-hosting guide (Compose file with Postgres and automatic HTTPS) will follow.

## Contributing

See [AGENTS.md](AGENTS.md) for architecture, conventions and rules (it applies to humans and AI coding agents alike).

## License

[MIT](LICENSE)
