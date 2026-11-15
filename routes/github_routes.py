"""GitHub connection API: each user's own token for Odysseus to act on GitHub with.

The token is stored encrypted and never returned; see ``src.github_access``.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src import github_access
from src.auth_helpers import get_current_user, require_user


class GitHubToken(BaseModel):
    token: str = Field(..., max_length=300)


def _owner(request: Request) -> Optional[str]:
    require_user(request)
    return get_current_user(request) or None


def _require_admin_or_single_user(request: Request) -> None:
    """Storing or removing a token needs the same privilege as a council workspace."""
    from src.tool_security import owner_is_admin_or_single_user

    if not owner_is_admin_or_single_user(get_current_user(request)):
        raise HTTPException(403, "Connecting GitHub needs an admin account.")


def setup_github_routes() -> APIRouter:
    router = APIRouter(prefix="/api/github", tags=["github"])

    @router.get("/connection")
    async def get_connection(request: Request):
        owner = _owner(request)
        return await asyncio.to_thread(github_access.connection, owner)

    @router.put("/connection")
    async def put_connection(request: Request, body: GitHubToken):
        owner = _owner(request)
        _require_admin_or_single_user(request)
        try:
            return await asyncio.to_thread(github_access.save_token, owner, body.token)
        except github_access.GitHubError as exc:
            raise HTTPException(exc.status, str(exc))

    @router.delete("/connection")
    async def delete_connection(request: Request):
        owner = _owner(request)
        _require_admin_or_single_user(request)
        return {"deleted": await asyncio.to_thread(github_access.forget_token, owner)}

    return router
