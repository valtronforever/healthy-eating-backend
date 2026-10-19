from fastapi import APIRouter

from healthy_eating.api.v1 import me, meals, settings

router = APIRouter(prefix="/api/v1")
router.include_router(me.router)
router.include_router(settings.router)
router.include_router(meals.router)
