"""Budget API: month-to-date metered spend and the user's guardrails."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import budget
from src.auth_helpers import get_current_user, require_user


class BudgetSettingsPatch(BaseModel):
    monthly_cap_usd: Optional[float] = None
    cap_action: Optional[str] = None
    action_limit_usd: Optional[float] = None


def _owner(request: Request) -> Optional[str]:
    require_user(request)
    return get_current_user(request) or None


def setup_budget_routes() -> APIRouter:
    router = APIRouter(prefix="/api/budget", tags=["budget"])

    @router.get("")
    async def get_summary(request: Request):
        owner = _owner(request)
        return await asyncio.to_thread(budget.summary, owner)

    @router.get("/status")
    async def get_status(request: Request):
        owner = _owner(request)
        return await asyncio.to_thread(budget.status, owner)

    @router.put("/settings")
    async def put_settings(request: Request, body: BudgetSettingsPatch):
        owner = _owner(request)
        try:
            await asyncio.to_thread(budget.save_settings, owner, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return await asyncio.to_thread(budget.summary, owner)

    return router
