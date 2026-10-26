"""Application factory. Run with: uvicorn --factory healthy_eating.main:create_app"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from healthy_eating import __version__
from healthy_eating.api import health, v1
from healthy_eating.api.errors import api_http_error, api_validation_error
from healthy_eating.api.openapi import v1_openapi
from healthy_eating.auth import routes as auth_routes
from healthy_eating.auth.provider import PostgresOAuthProvider
from healthy_eating.auth.social import SocialLogin
from healthy_eating.config import Settings
from healthy_eating.db import Database
from healthy_eating.mcp_server.server import create_mcp_server
from healthy_eating.services.cleanup import run_cleanup_periodically
from healthy_eating.services.reference_data import bundled_usda, ensure_reference_data

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level)

    db = Database(settings.database_url)
    oauth_provider = PostgresOAuthProvider(settings=settings, db=db)
    mcp_app = create_mcp_server(oauth_provider, db).http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Open reference data for vitamins and minerals; loaded only when the bundled file changed.
        bundled = await asyncio.to_thread(bundled_usda)
        async with db.session() as session:
            if await ensure_reference_data(session, bundled):
                logger.info("Loaded USDA reference data %s", bundled.version)
        cleanup = None
        if settings.cleanup_interval_seconds:
            cleanup = asyncio.create_task(
                run_cleanup_periodically(db, settings.cleanup_interval_seconds)
            )
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            if cleanup is not None:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
            await db.dispose()

    app = FastAPI(title="Healthy Eating", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.oauth_provider = oauth_provider
    app.state.social_login = SocialLogin(settings)

    # Holds only short-lived login state. Apple returns via a cross-site POST,
    # so over https the cookie must be SameSite=None to survive that redirect.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="healthy_eating_login",
        max_age=settings.login_ttl_seconds,
        same_site="none" if settings.is_https else "lax",
        https_only=settings.is_https,
    )

    app.add_exception_handler(HTTPException, api_http_error)
    app.add_exception_handler(RequestValidationError, api_validation_error)

    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(v1.router)
    app.add_api_route("/api/v1/openapi.json", v1_openapi, include_in_schema=False)
    # The MCP app serves /mcp plus the OAuth endpoints (/authorize, /token, /register,
    # /revoke, /.well-known/*). Mounted last so the routes above take precedence.
    app.mount("/", mcp_app)
    return app
