# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.10.5 /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Local development: dev dependencies, source bind-mounted by docker-compose, auto-reload.
FROM base AS dev
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory healthy_eating.main:create_app --host 0.0.0.0 --port 8000 --reload --reload-dir src"]

# Production: runtime dependencies only, non-root user.
FROM base AS prod
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY LICENSE alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
RUN useradd --system --uid 1000 --no-create-home app
USER app
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory healthy_eating.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
