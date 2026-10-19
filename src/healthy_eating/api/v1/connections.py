import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep, bearer_scheme
from healthy_eating.api.errors import error_response
from healthy_eating.db.base import utcnow
from healthy_eating.services.connections import (
    ConnectionKind,
    grant_of_access_token,
    list_connections,
    revoke_connection,
)

router = APIRouter(tags=["account"])

BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


class GrantResponse(BaseModel):
    grant_id: uuid.UUID
    created_at: datetime = Field(description="When this sign-in happened")
    last_used_at: datetime | None = Field(description="Last use, recorded at most once a minute")
    current: bool = Field(description="True for the sign-in making this request")


class ConnectionResponse(BaseModel):
    client_id: str
    name: str | None
    kind: ConnectionKind = Field(description="'ai_assistant' (MCP client) or 'ios_app'")
    grants: list[GrantResponse] = Field(description="Active sign-ins of this client, oldest first")


class ConnectionsResponse(BaseModel):
    connections: list[ConnectionResponse] = Field(description="Most recently used first")


class RevokeResponse(BaseModel):
    grant_id: uuid.UUID
    signed_out: bool = Field(
        description=(
            "True if this was the sign-in making the request: its tokens no longer work, "
            "so the app is signed out"
        )
    )


async def _current_grant(session: SessionDep, credentials: BearerCredentials) -> uuid.UUID | None:
    if credentials is None:
        return None
    return await grant_of_access_token(session, credentials.credentials)


@router.get("/connections", operation_id="listConnections")
async def read_connections(
    user_id: CurrentUserId, session: SessionDep, credentials: BearerCredentials
) -> ConnectionsResponse:
    """AI assistants and app sessions signed in to this account."""
    current = await _current_grant(session, credentials)
    clients = await list_connections(session, user_id, current_grant_id=current, now=utcnow())
    return ConnectionsResponse(
        connections=[
            ConnectionResponse(
                client_id=client.client_id,
                name=client.client_name,
                kind=client.kind,
                grants=[
                    GrantResponse(
                        grant_id=grant.grant_id,
                        created_at=grant.created_at,
                        last_used_at=grant.last_used_at,
                        current=grant.current,
                    )
                    for grant in client.grants
                ],
            )
            for client in clients
        ]
    )


@router.delete(
    "/connections/{grant_id}",
    operation_id="revokeConnection",
    responses={404: error_response("Not found")},
)
async def disconnect(
    grant_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep, credentials: BearerCredentials
) -> RevokeResponse:
    """Revoke one sign-in and all of its tokens. Revoking the current one signs the app out."""
    current = await _current_grant(session, credentials)
    if not await revoke_connection(session, user_id, grant_id, now=utcnow()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return RevokeResponse(grant_id=grant_id, signed_out=grant_id == current)
