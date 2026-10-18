import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.services.users import get_user

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
