"""Model roster API: every model the user can call, with recommendation and cost."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import model_roster
from src.auth_helpers import get_current_user, require_user

logger = logging.getLogger(__name__)


def setup_model_roster_routes() -> APIRouter:
    router = APIRouter(prefix="/api/models", tags=["models"])

    @router.get("/roster")
    async def get_roster(request: Request):
        require_user(request)
        owner = get_current_user(request) or None
        entries = await asyncio.to_thread(model_roster.roster, owner)
        return {
            "models": [e.to_dict() for e in entries],
            "prices": model_roster.PRICES.status(),
            "guidance": model_roster.ROUTING_GUIDANCE,
        }

    @router.post("/roster/refresh-prices")
    async def refresh_prices(request: Request):
        require_admin(request)
        try:
            count = await asyncio.to_thread(model_roster.PRICES.refresh)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
        return {"priced_models": count, "prices": model_roster.PRICES.status()}

    return router
