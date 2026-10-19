from fastapi import APIRouter

from healthy_eating.api.v1 import (
    connections,
    days,
    export,
    integration,
    me,
    meals,
    settings,
    trends,
)

router = APIRouter(prefix="/api/v1")
router.include_router(me.router)
router.include_router(settings.router)
router.include_router(meals.router)
router.include_router(days.router)
router.include_router(trends.router)
router.include_router(integration.router)
router.include_router(connections.router)
router.include_router(export.router)
