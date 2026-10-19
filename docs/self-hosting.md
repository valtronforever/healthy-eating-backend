# Self-hosting Healthy Eating

A Healthy Eating server is two containers: the app and PostgreSQL. You put an HTTPS reverse
proxy of your choice in front of it. The server needs no other service: no Redis, no external
identity provider, and no AI model (your AI assistant does the food recognition).

## What you need

- A Linux server (or any machine) with Docker and Docker Compose v2.
- A domain name, for example `food.example.com`, pointing to that server.
- An HTTPS reverse proxy (Caddy, nginx, Traefik, a Cloudflare tunnel, ...). MCP clients and
  the iOS app only connect over `https`.
- At least one social login: Google, Microsoft or Apple. Users sign in with it.

## 1. Get the code and configure it

```bash
git clone https://github.com/valtronforever/healthy-eating-backend.git
cd healthy-eating-backend
cp .env.production.example .env.production
```

Edit `.env.production`. It holds secrets: keep it private (it is git-ignored).

| Variable | Value |
|---|---|
| `BASE_URL` | Your public URL, e.g. `https://food.example.com` (no trailing slash) |
| `SECRET_KEY` | A random value: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `POSTGRES_PASSWORD` | Another random value, generated the same way |
| `APP_PORT` | Port on `127.0.0.1` the proxy forwards to (default `8000`) |
| `GOOGLE_*`, `MICROSOFT_*`, `APPLE_*` | See "Social login" below |

`POSTGRES_PASSWORD` is fixed when the database is created on the first start. Changing it
in `.env.production` later does not change the database password, and the app then cannot
connect. Choose it once.

The server refuses to start in production without an `https` `BASE_URL`, with a `SECRET_KEY`
shorter than 32 characters, or with `DEV_LOGIN_ENABLED=true`.

## 2. Social login

Register an OAuth application with each provider you want to offer and set all of its
variables. The callback URL is always `{BASE_URL}/auth/callback/{google|microsoft|apple}`,
for example `https://food.example.com/auth/callback/google`. Step-by-step instructions per
provider are in the [README](../README.md#social-login-setup).

Accounts are tied to the social login, never merged by email: signing in with Google and
later with Microsoft creates two separate accounts.

## 3. Start it

```bash
docker compose -f compose.production.yml --env-file .env.production up -d --build
docker compose -f compose.production.yml --env-file .env.production ps
curl http://127.0.0.1:8000/health      # {"status":"ok","database":"ok"}
```

Database migrations run automatically on every start. Both containers restart on failure and
after a reboot. Postgres is not reachable from outside the Compose network, and the app
listens only on `127.0.0.1`.

## 4. HTTPS reverse proxy

Point your proxy at `http://127.0.0.1:8000` (or your `APP_PORT`). It must pass the original
`Host` header and set `X-Forwarded-Proto` and `X-Forwarded-For`. Keep the app port off public
interfaces: the app trusts these headers.

Caddy (gets certificates automatically):

```
food.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

nginx (with your own certificate, e.g. from certbot):

```nginx
server {
    listen 443 ssl;
    server_name food.example.com;
    # ssl_certificate ...; ssl_certificate_key ...;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;          # MCP uses streaming HTTP responses
        proxy_read_timeout 300s;
    }
}
```

Check it from outside: `https://food.example.com/health` and
`https://food.example.com/.well-known/oauth-authorization-server` (its `issuer` must be your
`BASE_URL`).

## 5. Connect

- **AI assistant:** add a custom MCP connector with the URL `https://food.example.com/mcp`.
  The assistant opens your server's login page; sign in with a social account (the account is
  created on first sign-in).
- **iOS app:** enter `https://food.example.com` as the server address and sign in with the same
  social account.

Users can list and revoke connected assistants with `GET` and `DELETE /api/v1/connections`
(the iOS app's settings will show them).

## Backups

Back up the database regularly and keep copies off the server:

```bash
docker compose -f compose.production.yml --env-file .env.production \
  exec -T postgres pg_dump -U healthy -d healthy_eating -Fc > healthy-eating-$(date +%F).dump
```

Restore into an empty database (for example on a new server, after the first start):

```bash
docker compose -f compose.production.yml --env-file .env.production stop app
docker compose -f compose.production.yml --env-file .env.production exec -T postgres \
  pg_restore -U healthy -d healthy_eating --clean --if-exists --no-owner < healthy-eating-2026-10-19.dump
docker compose -f compose.production.yml --env-file .env.production start app
```

Also keep a copy of `.env.production`: without `SECRET_KEY`, `POSTGRES_PASSWORD` and the social
login secrets you cannot run the restored server.

## Upgrades

```bash
# Back up first (see above), then:
git pull
docker compose -f compose.production.yml --env-file .env.production up -d --build
```

Migrations run on start. The REST API is versioned (`/api/v1`), so an older iOS app keeps
working with a newer server.

## What the server stores

Health data is sensitive (in the EU it is special-category data under GDPR Art. 9). The server
stores, per account:

| Data | Details |
|---|---|
| Account | Display name and email from the social login |
| Logins | Provider, provider's user id, email, last login |
| Settings | Time zone, energy unit, calm mode, target ranges |
| Meals | Name, description, time, portion, calories and macronutrients, where the numbers came from (`source`, `estimated_fields`, `confidence`), and which assistant saved it |
| Day marks | Days marked complete or partial |
| Connections | Registered OAuth clients (assistants, the app) and sign-ins; tokens only as SHA-256 hashes |

The server never logs meal contents, nutrient values, tokens or email addresses. Users can
download all of their data (`GET /api/v1/export`, format in [export-format.md](export-format.md))
and delete their account with all data (`DELETE /api/v1/me`); the iOS app will offer both in
its settings.

## Troubleshooting

| Symptom | Check |
|---|---|
| App does not start | `docker compose -f compose.production.yml --env-file .env.production logs app`: production checks name the missing setting |
| `database` not ok in `/health` | Postgres container status; `POSTGRES_PASSWORD` changed after the first start |
| Assistant cannot connect | `BASE_URL` must be exactly the public `https` URL; the proxy must forward `/mcp` and `/.well-known/*` without buffering |
| Login page shows no providers | All variables of a provider must be set; restart after changing `.env.production` |
| Saving a meal feels slow | The app logs every MCP request, for example `MCP tools/call log_meal: ok in 6.1 ms (session be30b0dc request 3, 14.2 s after the previous, 20.5 s in, client ChatGPT b4341645)`. Server time is usually a few milliseconds. The gap after the previous request in the same session is time spent in the assistant (the model thinking, the user confirming the action). Clients on the stateless MCP version send no session; their requests are counted per client (`no session request N`) |
| Provider says redirect URI mismatch | Register `{BASE_URL}/auth/callback/<provider>` exactly |
