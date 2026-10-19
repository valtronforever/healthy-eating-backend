import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.services.users import delete_user, get_user

router = APIRouter(tags=["account"])


class MeResponse(BaseModel):
    id: uuid.UUID
    display_name: str | None
    email: str | None
    linked_logins: list[str]


@router.get("/me")
async def get_me(user_id: CurrentUserId, session: SessionDep) -> MeResponse:
    """The signed-in user's account."""
    user = await get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return MeResponse(
        id=user.id,
        display_name=user.display_name,
        email=user.primary_email,
        linked_logins=sorted(identity.provider for identity in user.identities),
    )


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["delete-account"] = Field(
        description="Must be 'delete-account', so an account is never deleted by accident"
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    body: DeleteAccountRequest, user_id: CurrentUserId, session: SessionDep
) -> Response:
    """Delete the account and all of its data, and end every sign-in. Cannot be undone."""
    if not await delete_user(session, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
