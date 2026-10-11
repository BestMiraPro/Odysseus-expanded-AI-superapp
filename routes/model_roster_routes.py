"""Model roster API: every model the user can call, with recommendation and cost."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import model_roster, reasoning_effort
from src.auth_helpers import effective_user, get_current_user, require_user

logger = logging.getLogger(__name__)


class EffortIn(BaseModel):
    model: str = Field(..., min_length=1, max_length=300)
    # The endpoint URL only selects which levels apply (provider detection);
    # nothing is fetched from it.
    url: str = Field(default="", max_length=2048)
    effort: Optional[str] = None


def _effort_state(owner: Optional[str], model: str, url: str) -> dict:
    from src.llm_core import _detect_provider

    supported = reasoning_effort.supported_levels(_detect_provider(url), model, url)
    saved = reasoning_effort.user_levels(owner).get(model)
    return {
        "model": model,
        "levels": [{"id": lvl, "label": reasoning_effort.LABELS[lvl]} for lvl in supported],
        "effort": saved,
        # What is actually sent: the saved level clamped to this model's range.
        "applied": reasoning_effort.clamp(saved, supported),
    }


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

    @router.get("/effort")
    async def get_effort(request: Request, model: str, url: str = ""):
        """Effort levels ``model`` accepts on this endpoint, and the user's choice."""
        require_user(request)
        if not model.strip() or len(model) > 300 or len(url) > 2048:
            raise HTTPException(400, "Invalid model or endpoint")
        return await asyncio.to_thread(_effort_state, effective_user(request), model.strip(), url)

    @router.put("/effort")
    async def set_effort(request: Request, body: EffortIn):
        """Save (or with ``effort: null`` clear) the user's effort for a model."""
        require_user(request)
        level = None
        if body.effort not in (None, "", "default"):
            level = reasoning_effort.normalize_level(body.effort)
            if not level:
                raise HTTPException(400, "Unknown effort level")
        owner = effective_user(request)
        model = body.model.strip()
        await asyncio.to_thread(reasoning_effort.save_user_level, owner, model, level)
        return await asyncio.to_thread(_effort_state, owner, model, body.url)

    @router.post("/roster/refresh-prices")
    async def refresh_prices(request: Request):
        require_admin(request)
        try:
            count = await asyncio.to_thread(model_roster.PRICES.refresh)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
        return {"priced_models": count, "prices": model_roster.PRICES.status()}

    return router
