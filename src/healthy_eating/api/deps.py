import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.auth.provider import PostgresOAuthProvider
from healthy_eating.db import Database

bearer_scheme = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    db: Database = request.app.state.db
    async with db.session() as session:
        yield session


async def get_current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> uuid.UUID:
    """Authenticate the request with an access token issued by our authorization server."""
    provider: PostgresOAuthProvider = request.app.state.oauth_provider
    token = await provider.load_access_token(credentials.credentials) if credentials else None
    if token is None or token.subject is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return uuid.UUID(token.subject)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserId = Annotated[uuid.UUID, Depends(get_current_user_id)]
