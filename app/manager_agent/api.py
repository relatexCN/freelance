"""Optional authenticated management endpoints; no mutation operations."""

from fastapi import APIRouter, Depends, HTTPException, Request
from app.config import config
from .monitor import snapshot
from .service import manager_agent


async def require_manager(request: Request):
    if not config.settings.manager_agent.enabled:
        raise HTTPException(404, "Management agent is disabled")
    keys = {k.strip() for k in config.settings.api_keys.split(",") if k.strip()}
    if config.settings.api_key:
        keys.add(config.settings.api_key)
    if not keys:
        raise HTTPException(503, "Configure a gateway API key before enabling management")
    if request.headers.get("authorization", "") not in {f"Bearer {key}" for key in keys}:
        raise HTTPException(401, "Invalid or missing API key")


router = APIRouter(prefix="/management", dependencies=[Depends(require_manager)])


@router.get("/snapshot")
async def get_snapshot():
    return snapshot()


@router.post("/report")
async def create_report():
    if manager_agent.lock.locked():
        raise HTTPException(409, "A management report is already running")
    async with manager_agent.lock:
        return await manager_agent.report()
