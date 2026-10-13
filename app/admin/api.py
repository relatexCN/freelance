"""Authenticated configuration endpoints independent of manager_agent.enabled."""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import config, find_config_file, ENV_PATH, EXTERNAL_ENV_NAMES
from app.manager_agent.monitor import snapshot
from .store import ConfigStore, ConflictError, ProviderEdit, AliasEdit


store = ConfigStore(find_config_file("config.yaml"), ENV_PATH, EXTERNAL_ENV_NAMES)


async def require_admin(request: Request):
    key = config.settings.admin_api_key
    if not key:
        raise HTTPException(503, "Configure ADMIN_API_KEY to enable administration")
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not secrets.compare_digest(token.encode(), key.encode()):
        raise HTTPException(401, "Invalid administrator key")


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    provider: ProviderEdit


class AliasUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    route: AliasEdit


class AliasDelete(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    alias: str


def perform(operation):
    try:
        return operation()
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, OSError):
        # Never echo configuration contents, filesystem paths or secrets.
        raise HTTPException(400, "Unable to read or save configuration; check fields and server file permissions") from None


@router.get("/status")
async def status(request: Request):
    return {"status": request.app.state.startup_status}


@router.get("/snapshot")
def get_snapshot():
    return snapshot()


@router.get("/config")
def get_config():
    return perform(store.read)


@router.put("/providers")
def update_provider(body: ProviderUpdate):
    return perform(lambda: store.provider(body.revision, body.provider))


@router.put("/aliases")
def update_alias(body: AliasUpdate):
    return perform(lambda: store.alias(body.revision, body.route))


@router.delete("/aliases")
def delete_alias(body: AliasDelete):
    return perform(lambda: store.delete_alias(body.revision, body.alias))
