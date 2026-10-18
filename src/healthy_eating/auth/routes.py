"""Browser-facing login pages: provider picker, social callbacks, dev login."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from healthy_eating.auth.provider import LoginRequestNotFoundError, PostgresOAuthProvider
from healthy_eating.auth.social import PROVIDER_LABELS, SocialLogin, SocialLoginError
from healthy_eating.config import Settings
from healthy_eating.db import Database
from healthy_eating.services.users import SocialProfile, find_or_create_user

SESSION_KEY = "login_request"

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _error(request: Request, message: str, status_code: int = 400) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=status_code
    )


async def _finish_login(request: Request, profile: SocialProfile) -> Response:
    """Link the social identity to a user and send the browser back to the OAuth client."""
    request_id = request.session.pop(SESSION_KEY, None)
    if not request_id:
        return _error(request, "Your sign-in session has expired. Please start again.")
    db: Database = request.app.state.db
    provider: PostgresOAuthProvider = request.app.state.oauth_provider
    async with db.session() as session:
        user = await find_or_create_user(session, profile)
    try:
        redirect_url = await provider.complete_login(request_id, user.id)
    except LoginRequestNotFoundError:
        return _error(request, "Your sign-in request has expired. Please start again.")
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/login")
async def login_page(
    request: Request, request_id: Annotated[str, Query(alias="request")] = ""
) -> Response:
    provider: PostgresOAuthProvider = request.app.state.oauth_provider
    social: SocialLogin = request.app.state.social_login
    settings: Settings = request.app.state.settings
    try:
        pending = await provider.get_pending_login(request_id)
    except LoginRequestNotFoundError:
        return _error(request, "This sign-in link is invalid or has expired.")

    # Bind the login request to this browser, so only it can complete the login.
    request.session[SESSION_KEY] = request_id
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "pending": pending,
            "providers": [(p, PROVIDER_LABELS[p]) for p in social.enabled_providers],
            "dev_login_enabled": settings.dev_login_enabled,
        },
    )


@router.get("/login/cancel")
async def cancel_login(request: Request) -> Response:
    request_id = request.session.pop(SESSION_KEY, None)
    provider: PostgresOAuthProvider = request.app.state.oauth_provider
    if not request_id:
        return _error(request, "Your sign-in session has expired.")
    try:
        redirect_url = await provider.cancel_login(request_id)
    except LoginRequestNotFoundError:
        return _error(request, "Your sign-in request has expired.")
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/auth/start/{provider_name}")
async def start_social_login(request: Request, provider_name: str) -> Response:
    if SESSION_KEY not in request.session:
        return _error(request, "Your sign-in session has expired. Please start again.")
    social: SocialLogin = request.app.state.social_login
    try:
        return await social.start(request, provider_name)
    except SocialLoginError:
        raise HTTPException(status_code=404) from None


# GET for Google and Microsoft, POST (form_post) for Apple.
@router.api_route("/auth/callback/{provider_name}", methods=["GET", "POST"])
async def social_callback(request: Request, provider_name: str) -> Response:
    social: SocialLogin = request.app.state.social_login
    try:
        profile = await social.finish(request, provider_name)
    except SocialLoginError:
        return _error(request, "Sign-in with the provider failed. Please try again.")
    return await _finish_login(request, profile)


@router.post("/auth/dev-login")
async def dev_login(request: Request, email: Annotated[str, Form()]) -> Response:
    settings: Settings = request.app.state.settings
    if not settings.dev_login_enabled:
        raise HTTPException(status_code=404)
    email = email.strip().lower()
    if not email:
        return _error(request, "Email is required.")
    profile = SocialProfile(
        provider="dev",
        subject=email,
        email=email,
        email_verified=True,
        display_name=email.split("@")[0],
    )
    return await _finish_login(request, profile)
