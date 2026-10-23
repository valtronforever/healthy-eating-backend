"""The signed-in user's social logins: list, add one, remove one (healthy-eating-backend#20)."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.config import Settings
from healthy_eating.db.base import utcnow
from healthy_eating.services.linking import (
    LinkResult,
    UnlinkResult,
    create_link_request,
    finish_link,
    list_logins,
    unlink_login,
)

router = APIRouter(prefix="/me/logins", tags=["account"])


class Login(BaseModel):
    id: uuid.UUID
    provider: str = Field(description="google, microsoft, apple (dev on development servers)")
    email: str | None = Field(description="The email the provider gave, if any")
    created_at: datetime = Field(description="When it was added to the account")
    last_login_at: datetime | None


class LoginList(BaseModel):
    logins: list[Login] = Field(description="Oldest first")


@router.get("", operation_id="listLogins")
async def get_logins(user_id: CurrentUserId, session: SessionDep) -> LoginList:
    """The social logins that sign in to this account."""
    identities = await list_logins(session, user_id)
    return LoginList(
        logins=[
            Login(
                id=identity.id,
                provider=identity.provider,
                email=identity.email,
                created_at=identity.created_at,
                last_login_at=identity.last_login_at,
            )
            for identity in identities
        ]
    )


class LinkStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_challenge: str = Field(
        min_length=43,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="PKCE S256 challenge of a verifier only the app knows",
    )
    code_challenge_method: Literal["S256"] = "S256"


class LinkStartResponse(BaseModel):
    url: str = Field(
        description=(
            "Open in the system web sheet. The user signs in with the login to add; the "
            "browser then goes to healthyeating://link/callback with `code` (finish with "
            "completeLoginLink) or `error` (cancelled or expired)"
        )
    )
    expires_at: datetime


@router.post("/link", operation_id="startLoginLink", status_code=status.HTTP_201_CREATED)
async def start_link(
    body: LinkStart, request: Request, user_id: CurrentUserId, session: SessionDep
) -> LinkStartResponse:
    """Start adding another social login to this account. Accounts are never linked by email."""
    settings: Settings = request.app.state.settings
    link = await create_link_request(session, user_id, body.code_challenge, now=utcnow())
    return LinkStartResponse(
        url=f"{settings.base_url.rstrip('/')}/link?request={link.id}", expires_at=link.expires_at
    )


class LinkComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=128, description="`code` from healthyeating://link/callback")
    code_verifier: str = Field(min_length=43, max_length=128)


class LinkCompleteResponse(BaseModel):
    result: Literal["linked", "already_linked"] = Field(
        description="already_linked: the login was already one of this account's logins"
    )


@router.post(
    "/link/complete",
    operation_id="completeLoginLink",
    responses={
        400: error_response("The code is unknown, used, expired or does not match"),
        409: error_response("The login belongs to another Healthy Eating account"),
    },
)
async def complete_link(
    body: LinkComplete, user_id: CurrentUserId, session: SessionDep
) -> LinkCompleteResponse:
    """Finish adding a login. Each code works once, also when it is refused."""
    result = await finish_link(session, user_id, body.code, body.code_verifier, now=utcnow())
    if result is LinkResult.LINKED or result is LinkResult.ALREADY_LINKED:
        return LinkCompleteResponse(result=result.value)
    # Keep the used-up link request deleted although the request fails.
    await session.commit()
    if result is LinkResult.OTHER_ACCOUNT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This login already belongs to another Healthy Eating account. Accounts are "
                "never merged; delete the other account first to add this login here."
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This link has expired or was already used. Start again.",
    )


@router.delete(
    "/{login_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteLogin",
    responses={
        404: error_response("Not found"),
        409: error_response("The account's only login"),
    },
)
async def delete_login(
    login_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep
) -> Response:
    """Remove a login from this account. The last login cannot be removed."""
    result = await unlink_login(session, user_id, login_id)
    if result is UnlinkResult.NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if result is UnlinkResult.LAST_LOGIN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is the only login of the account, so it cannot be removed.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
