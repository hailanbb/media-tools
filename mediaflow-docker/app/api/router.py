"""顶层 API 路由聚合."""

from fastapi import APIRouter

from app.api import archive, inspection, moviepilot, system, tasks

api_router = APIRouter(prefix="/api")

api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(inspection.router, prefix="/inspect", tags=["inspection"])
api_router.include_router(archive.router, prefix="/archive", tags=["archive"])
api_router.include_router(moviepilot.router, prefix="/moviepilot", tags=["moviepilot"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
