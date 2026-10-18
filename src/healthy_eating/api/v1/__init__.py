from fastapi import APIRouter

from healthy_eating.api.v1 import me

router = APIRouter(prefix="/api/v1")
router.include_router(me.router)
