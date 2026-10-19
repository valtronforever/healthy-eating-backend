from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.db.base import utcnow
from healthy_eating.services.integration import integration_status

router = APIRouter(tags=["account"])


class IntegrationStatusResponse(BaseModel):
    last_meal_saved_at: datetime | None = Field(
        description="When the latest meal reached Healthy Eating (not when it was eaten)"
    )
    last_client_name: str | None = Field(description="The AI assistant that saved it, if known")
    meals_today: int = Field(description="Meals eaten on the user's local today")


@router.get("/integration-status", operation_id="getIntegrationStatus")
async def read_integration_status(
    user_id: CurrentUserId, session: SessionDep
) -> IntegrationStatusResponse:
    """Whether meals are arriving from the AI assistant. Contains no meal contents."""
    status = await integration_status(session, user_id, now=utcnow())
    return IntegrationStatusResponse(
        last_meal_saved_at=status.last_meal_saved_at,
        last_client_name=status.last_client_name,
        meals_today=status.meals_today,
    )
