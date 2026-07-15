"""WebUI product package."""

from fastapi import APIRouter

from .chat import router as chat_router
from .imagine import router as imagine_router
from .pages import router as pages_router
from .voice import router as voice_router

router = APIRouter()
router.include_router(chat_router)
router.include_router(imagine_router, prefix="/webui/api")
router.include_router(voice_router)
router.include_router(pages_router)

__all__ = ["router"]
