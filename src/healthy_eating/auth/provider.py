"""Our OAuth 2.1 authorization server, backed by Postgres.

FastMCP's `OAuthProvider` serves the protocol endpoints (/authorize, /token, /register,
/revoke and the discovery metadata) and enforces PKCE. This class supplies storage and
hands the user over to our login page, where they sign in with a social provider.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.redirect_validation import is_redirect_uri_allowed_for_application_type
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.auth.tokens import generate_token, hash_token
from healthy_eating.config import Settings
from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import (
    AccessTokenRecord,
    AuthorizationCodeRecord,
    Grant,
    LoginRequest,
    OAuthClient,
    RefreshTokenRecord,
)
from healthy_eating.services.connections import record_grant_use, revoke_grant

# Access to the user's nutrition data: every token carries it (the MCP endpoint requires it).
NUTRITION_SCOPE = "nutrition"
# Asks for a refresh token (OpenID Connect). Refresh tokens are always issued; the scope is
# advertised and accepted because some clients (ChatGPT) only refresh when it is listed.
OFFLINE_ACCESS_SCOPE = "offline_access"
SCOPES = [NUTRITION_SCOPE, OFFLINE_ACCESS_SCOPE]
REQUIRED_SCOPES = [NUTRITION_SCOPE]


def granted_scopes(requested: list[str] | None) -> list[str]:
    """The scopes of a sign-in: what the client asked for, always including `nutrition`."""
    scopes = list(requested or SCOPES)
    if NUTRITION_SCOPE not in scopes:
        scopes.insert(0, NUTRITION_SCOPE)
    return scopes


class LoginRequestNotFoundError(Exception):
    """The login request does not exist, was already used, or has expired."""


@dataclass(frozen=True)
class PendingLogin:
    """What the login page needs to show the user who is asking for access."""

    request_id: str
    client_name: str
    redirect_host: str


class PostgresOAuthProvider(OAuthProvider):
    def __init__(self, *, settings: Settings, db: Database) -> None:
        super().__init__(
            base_url=settings.public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=REQUIRED_SCOPES,
        )
        self._settings = settings
        self._db = db

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._db.session() as session:
            record = await session.get(OAuthClient, client_id)
            if record is None:
                return None
            return OAuthClientInformationFull.model_validate(record.client_info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not is_redirect_uri_allowed_for_application_type(uri, client_info.application_type):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"Redirect URI not allowed: {uri}",
                )
        assert client_info.client_id is not None
        async with self._db.session() as session:
            session.add(
                OAuthClient(
                    client_id=client_info.client_id,
                    client_info=client_info.model_dump(mode="json", exclude_none=True),
                )
            )

    # Authorization: /authorize -> login page -> social login -> authorization code

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and not params.resource.startswith(self._settings.public_url):
            raise AuthorizeError(
                error="invalid_target", error_description="Unknown resource indicator"
            )
        assert client.client_id is not None
        request_id = generate_token()
        async with self._db.session() as session:
            session.add(
                LoginRequest(
                    id=request_id,
                    client_id=client.client_id,
                    redirect_uri=str(params.redirect_uri),
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    code_challenge=params.code_challenge,
                    state=params.state,
                    scopes=granted_scopes(params.scopes),
                    resource=params.resource,
                    expires_at=utcnow() + timedelta(seconds=self._settings.login_ttl_seconds),
                )
            )
        return f"{self._settings.public_url}/login?{urlencode({'request': request_id})}"

    async def get_pending_login(self, request_id: str) -> PendingLogin:
        async with self._db.session() as session:
            login = await self._get_active_login(session, request_id)
            client = await session.get(OAuthClient, login.client_id)
            client_name = (client.client_info.get("client_name") if client else None) or (
                "Unknown application"
            )
            return PendingLogin(
                request_id=request_id,
                client_name=client_name,
                redirect_host=AnyUrl(login.redirect_uri).host or login.redirect_uri,
            )

    async def complete_login(self, request_id: str, user_id: uuid.UUID) -> str:
        """Issue an authorization code and return the client redirect URL."""
        code = generate_token()
        async with self._db.session() as session:
            login = await self._get_active_login(session, request_id, for_update=True)
            session.add(
                AuthorizationCodeRecord(
                    code_hash=hash_token(code),
                    client_id=login.client_id,
                    user_id=user_id,
                    redirect_uri=login.redirect_uri,
                    redirect_uri_provided_explicitly=login.redirect_uri_provided_explicitly,
                    code_challenge=login.code_challenge,
                    scopes=login.scopes,
                    resource=login.resource,
                    expires_at=utcnow()
                    + timedelta(seconds=self._settings.authorization_code_ttl_seconds),
                )
            )
            await session.delete(login)
        return construct_redirect_uri(login.redirect_uri, code=code, state=login.state)

    async def cancel_login(self, request_id: str) -> str:
        """Drop the login request and return the client redirect URL with access_denied."""
        async with self._db.session() as session:
            login = await self._get_active_login(session, request_id, for_update=True)
            await session.delete(login)
        return construct_redirect_uri(login.redirect_uri, error="access_denied", state=login.state)

    async def _get_active_login(
        self, session: AsyncSession, request_id: str, *, for_update: bool = False
    ) -> LoginRequest:
        login = await session.get(LoginRequest, request_id, with_for_update=for_update)
        if login is None or login.expires_at <= utcnow():
            raise LoginRequestNotFoundError
        return login

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with self._db.session() as session:
            record = await session.get(AuthorizationCodeRecord, hash_token(authorization_code))
        if record is None or record.client_id != client.client_id:
            return None
        if record.expires_at <= utcnow():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=record.scopes,
            expires_at=record.expires_at.timestamp(),
            client_id=record.client_id,
            code_challenge=record.code_challenge,
            redirect_uri=AnyUrl(record.redirect_uri),
            redirect_uri_provided_explicitly=record.redirect_uri_provided_explicitly,
            resource=record.resource,
            subject=str(record.user_id),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with self._db.session() as session:
            # Deleting with RETURNING makes the code single-use even under concurrent requests.
            user_id = await session.scalar(
                delete(AuthorizationCodeRecord)
                .where(AuthorizationCodeRecord.code_hash == hash_token(authorization_code.code))
                .returning(AuthorizationCodeRecord.user_id)
            )
            if user_id is None:
                raise TokenError(error="invalid_grant", error_description="Code already used")
            grant_id = uuid.uuid4()
            session.add(Grant(id=grant_id, user_id=user_id, client_id=authorization_code.client_id))
            return self._issue_tokens(
                session,
                grant_id=grant_id,
                client_id=authorization_code.client_id,
                user_id=user_id,
                scopes=authorization_code.scopes,
                resource=authorization_code.resource,
            )

    # Refresh tokens (rotated on every use)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        async with self._db.session() as session:
            record = await session.get(RefreshTokenRecord, hash_token(refresh_token))
            if record is None or record.client_id != client.client_id:
                return None
            if record.revoked_at is not None:
                # A rotated token was presented again: assume it leaked and end the grant.
                await self._revoke_grant(session, record.grant_id)
                return None
            if record.expires_at <= utcnow():
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=record.client_id,
                scopes=record.scopes,
                expires_at=int(record.expires_at.timestamp()),
                resource=record.resource,
                subject=str(record.user_id),
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._db.session() as session:
            record = await session.scalar(
                select(RefreshTokenRecord)
                .where(
                    RefreshTokenRecord.token_hash == hash_token(refresh_token.token),
                    RefreshTokenRecord.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if record is None:
                raise TokenError(error="invalid_grant", error_description="Refresh token revoked")
            record.revoked_at = utcnow()
            return self._issue_tokens(
                session,
                grant_id=record.grant_id,
                client_id=record.client_id,
                user_id=record.user_id,
                scopes=scopes or record.scopes,
                resource=record.resource,
            )

    # Access tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        now = utcnow()
        async with self._db.session() as session:
            record = await session.get(AccessTokenRecord, hash_token(token))
            if record is None or record.revoked_at is not None or record.expires_at <= now:
                return None
            await record_grant_use(session, record.grant_id, now=now)
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=int(record.expires_at.timestamp()),
            resource=record.resource,
            subject=str(record.user_id),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        model = RefreshTokenRecord if isinstance(token, RefreshToken) else AccessTokenRecord
        async with self._db.session() as session:
            grant_id = await session.scalar(
                select(model.grant_id).where(model.token_hash == hash_token(token.token))
            )
            if grant_id is not None:
                await self._revoke_grant(session, grant_id)

    # Helpers

    def _issue_tokens(
        self,
        session: AsyncSession,
        *,
        grant_id: uuid.UUID,
        client_id: str,
        user_id: uuid.UUID,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        now = utcnow()
        access_token = generate_token()
        refresh_token = generate_token()
        session.add_all(
            [
                AccessTokenRecord(
                    token_hash=hash_token(access_token),
                    grant_id=grant_id,
                    client_id=client_id,
                    user_id=user_id,
                    scopes=scopes,
                    resource=resource,
                    expires_at=now + timedelta(seconds=self._settings.access_token_ttl_seconds),
                ),
                RefreshTokenRecord(
                    token_hash=hash_token(refresh_token),
                    grant_id=grant_id,
                    client_id=client_id,
                    user_id=user_id,
                    scopes=scopes,
                    resource=resource,
                    expires_at=now + timedelta(seconds=self._settings.refresh_token_ttl_seconds),
                ),
            ]
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=self._settings.access_token_ttl_seconds,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    async def _revoke_grant(self, session: AsyncSession, grant_id: uuid.UUID) -> None:
        await revoke_grant(session, grant_id, now=utcnow())
