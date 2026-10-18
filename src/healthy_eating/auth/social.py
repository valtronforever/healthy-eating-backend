"""Social login (Google, Microsoft, Apple) used only to identify the user.

Upstream tokens are never stored or passed on: we read the verified ID token claims,
turn them into a `SocialProfile`, and issue our own tokens.
"""

import json
import time
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from joserfc import jwt
from joserfc.jwk import ECKey
from starlette.requests import Request
from starlette.responses import Response

from healthy_eating.config import Settings
from healthy_eating.services.users import SocialProfile

PROVIDER_LABELS = {"google": "Google", "microsoft": "Microsoft", "apple": "Apple"}

APPLE_ISSUER = "https://appleid.apple.com"


class SocialLoginError(Exception):
    pass


def _validate_microsoft_issuer(claims: dict[str, Any], value: str) -> bool:
    # The `common` endpoint serves many tenants, so the issuer contains the user's tenant id.
    return value == f"https://login.microsoftonline.com/{claims.get('tid')}/v2.0"


def _as_bool(value: object) -> bool:
    # Apple sends booleans as strings ("true") in some claims.
    return value is True or value == "true"


class SocialLogin:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._oauth = OAuth()
        self._enabled: list[str] = []

        if settings.google_enabled:
            assert settings.google_client_secret is not None
            self._oauth.register(
                "google",
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret.get_secret_value(),
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
            )
            self._enabled.append("google")
        if settings.microsoft_enabled:
            assert settings.microsoft_client_secret is not None
            self._oauth.register(
                "microsoft",
                client_id=settings.microsoft_client_id,
                client_secret=settings.microsoft_client_secret.get_secret_value(),
                server_metadata_url=(
                    "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
                ),
                client_kwargs={"scope": "openid email profile"},
            )
            self._enabled.append("microsoft")
        if settings.apple_enabled:
            self._oauth.register(
                "apple",
                client_id=settings.apple_services_id,
                # The client secret is a short-lived JWT, generated before each token request.
                client_secret=None,
                server_metadata_url=f"{APPLE_ISSUER}/.well-known/openid-configuration",
                client_kwargs={
                    "scope": "openid email name",
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
            self._enabled.append("apple")

    @property
    def enabled_providers(self) -> list[str]:
        return list(self._enabled)

    def _client(self, provider: str) -> Any:
        if provider not in self.enabled_providers:
            raise SocialLoginError(f"Provider not enabled: {provider}")
        return self._oauth.create_client(provider)

    def _callback_url(self, provider: str) -> str:
        return f"{self._settings.public_url}/auth/callback/{provider}"

    async def start(self, request: Request, provider: str) -> Response:
        client = self._client(provider)
        extra: dict[str, str] = {}
        if provider == "apple":
            # Apple requires form_post when the `name` or `email` scope is requested.
            extra["response_mode"] = "form_post"
        response: Response = await client.authorize_redirect(
            request, self._callback_url(provider), **extra
        )
        return response

    async def finish(self, request: Request, provider: str) -> SocialProfile:
        client = self._client(provider)
        options: dict[str, Any] = {}
        if provider == "apple":
            client.client_secret = self._apple_client_secret()
        if provider == "microsoft":
            options["claims_options"] = {
                "iss": {"essential": True, "validate": _validate_microsoft_issuer}
            }
        try:
            token = await client.authorize_access_token(request, **options)
        except OAuthError as exc:
            raise SocialLoginError(str(exc)) from exc

        claims = token.get("userinfo")
        if not claims or not claims.get("sub"):
            raise SocialLoginError("Provider did not return an ID token")

        if provider == "apple":
            return await self._apple_profile(request, claims)
        return SocialProfile(
            provider=provider,
            subject=str(claims["sub"]),
            email=claims.get("email"),
            # Microsoft does not guarantee that `email` is verified, so it never counts as such.
            email_verified=provider == "google" and _as_bool(claims.get("email_verified")),
            display_name=claims.get("name"),
        )

    async def _apple_profile(self, request: Request, claims: dict[str, Any]) -> SocialProfile:
        display_name = None
        # Apple sends the user's name only on the very first sign-in, as a form field.
        form = await request.form()
        raw_user = form.get("user")
        if isinstance(raw_user, str):
            name = json.loads(raw_user).get("name") or {}
            parts = [name.get("firstName"), name.get("lastName")]
            display_name = " ".join(p for p in parts if p) or None
        return SocialProfile(
            provider="apple",
            subject=str(claims["sub"]),
            email=claims.get("email"),
            email_verified=_as_bool(claims.get("email_verified")),
            display_name=display_name,
        )

    def _apple_client_secret(self) -> str:
        settings = self._settings
        assert settings.apple_private_key is not None
        pem = settings.apple_private_key.get_secret_value().replace("\\n", "\n")
        now = int(time.time())
        return jwt.encode(
            {"alg": "ES256", "kid": settings.apple_key_id},
            {
                "iss": settings.apple_team_id,
                "iat": now,
                "exp": now + 300,
                "aud": APPLE_ISSUER,
                "sub": settings.apple_services_id,
            },
            ECKey.import_key(pem),
        )
