from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep, bearer_scheme
from healthy_eating.api.errors import error_response
from healthy_eating.api.v1.settings import TargetRangeModel, Targets
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, EnergyUnit
from healthy_eating.services.connections import grant_of_access_token
from healthy_eating.services.widget import issue_widget_token, widget_today, widget_user

router = APIRouter(tags=["widget"])

BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]

widget_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="WidgetToken",
    description=(
        "A widget token from POST /api/v1/widget-token. It reads only /api/v1/widget/today "
        "and ends with the app sign-in it was issued for"
    ),
)
WidgetCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(widget_scheme)]


class WidgetTokenResponse(BaseModel):
    token: str = Field(
        description=(
            "Opaque read-only token for GET /api/v1/widget/today. Replaces an earlier one of the "
            "same sign-in; stops working when that sign-in is revoked"
        )
    )


class WidgetTodayResponse(BaseModel):
    local_date: date = Field(description="The user's local today")
    meal_count: int
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    targets: Targets
    energy_unit: EnergyUnit = Field(description="Display unit; values stay in kcal")
    calm_mode: bool = Field(description="True: the widgets show no calories")
    last_meal_saved_at: datetime | None = Field(
        description="When the latest meal reached Healthy Eating"
    )
    last_client_name: str | None = Field(description="The AI assistant that saved it, if known")


@router.post(
    "/widget-token",
    operation_id="createWidgetToken",
    responses={400: error_response("Not an app sign-in")},
)
async def create_widget_token(
    user_id: CurrentUserId, session: SessionDep, credentials: BearerCredentials
) -> WidgetTokenResponse:
    """A read-only token for the iOS widgets, tied to the sign-in making this request."""
    grant_id = (
        None
        if credentials is None
        else await grant_of_access_token(session, credentials.credentials)
    )
    if grant_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No sign-in for this token")
    return WidgetTokenResponse(token=await issue_widget_token(session, user_id, grant_id))


@router.get("/widget/today", operation_id="getWidgetToday")
async def read_widget_today(
    session: SessionDep, credentials: WidgetCredentials
) -> WidgetTodayResponse:
    """Today's totals for the iOS widgets. Accepts only a widget token."""
    now = utcnow()
    user_id = (
        None
        if credentials is None
        else await widget_user(session, credentials.credentials, now=now)
    )
    if user_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    today = await widget_today(session, user_id, now=now)
    targets = {
        name: None if target is None else TargetRangeModel(min=target.min, max=target.max)
        for name, target in today.settings.targets.items()
        if name in NUTRIENT_FIELDS
    }
    return WidgetTodayResponse(
        local_date=today.day.local_date,
        meal_count=today.day.meal_count,
        calories_kcal=today.day.calories_kcal,
        protein_g=today.day.protein_g,
        fat_g=today.day.fat_g,
        carbohydrates_g=today.day.carbohydrates_g,
        targets=Targets(**targets),
        energy_unit=today.settings.energy_unit,
        calm_mode=today.settings.calm_mode,
        last_meal_saved_at=today.last_meal_saved_at,
        last_client_name=today.last_client_name,
    )
