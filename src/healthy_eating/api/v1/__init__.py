from fastapi import APIRouter

from healthy_eating.api.errors import error_response
from healthy_eating.api.v1 import (
    connections,
    days,
    export,
    integration,
    me,
    meals,
    settings,
    trends,
    weekly_review,
)

router = APIRouter(
    prefix="/api/v1",
    responses={
        401: error_response("Missing, invalid or expired access token"),
        422: error_response("The request is not valid"),
    },
)
router.include_router(me.router)
router.include_router(settings.router)
router.include_router(meals.router)
router.include_router(days.router)
router.include_router(trends.router)
router.include_router(weekly_review.router)
router.include_router(integration.router)
router.include_router(connections.router)
router.include_router(export.router)
