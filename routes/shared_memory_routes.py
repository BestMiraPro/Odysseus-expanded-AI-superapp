"""Shared memory API: each user's own memory-repository settings.

The repository is read read-only through GitHub; see ``src.shared_memory``.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src import shared_memory
from src.auth_helpers import get_current_user, require_user


class SharedMemorySettings(BaseModel):
    enabled: bool = False
    repo: str = Field("", max_length=300)
    branch: str = Field("main", max_length=200)
    files: Union[List[str], str] = Field(default_factory=lambda: list(shared_memory.DEFAULT_FILES))
    # None keeps the stored token, "" removes it, anything else replaces it.
    token: Optional[str] = Field(None, max_length=500)


def _owner(request: Request) -> Optional[str]:
    require_user(request)
    return get_current_user(request) or None


def _payload(owner: Optional[str]) -> dict:
    return {"config": shared_memory.load_config(owner).public(), "status": shared_memory.status(owner)}


def setup_shared_memory_routes() -> APIRouter:
    router = APIRouter(prefix="/api/shared-memory", tags=["memory"])

    @router.get("")
    async def get_settings(request: Request):
        owner = _owner(request)
        return await asyncio.to_thread(_payload, owner)

    @router.put("")
    async def put_settings(request: Request, body: SharedMemorySettings):
        owner = _owner(request)

        def save() -> dict:
            shared_memory.save_config(owner, enabled=body.enabled, repo=body.repo, branch=body.branch,
                                      files=body.files, token=body.token)
            shared_memory.refresh(owner)
            return _payload(owner)

        try:
            return await asyncio.to_thread(save)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.post("/refresh")
    async def refresh(request: Request):
        owner = _owner(request)

        def run() -> dict:
            shared_memory.refresh(owner)
            return _payload(owner)

        return await asyncio.to_thread(run)

    return router
