"""Budget API: month-to-date metered spend and the user's guardrails."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src import budget
from src.auth_helpers import get_current_user, require_user


class PriceIn(BaseModel):
    model: str = Field(..., min_length=1, max_length=300)
    # USD per 1M tokens; both None clears the declared price.
    input_per_mtok: Optional[float] = Field(None, ge=0, le=100000)
    output_per_mtok: Optional[float] = Field(None, ge=0, le=100000)


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

    def _price_rows(owner):
        from src import model_roster

        rows = {}
        for entry in model_roster.roster(owner):
            if entry.billing != "metered":
                continue
            row = rows.setdefault(entry.model, {
                "model": entry.model, "endpoints": [], "input_per_mtok": entry.input_per_mtok,
                "output_per_mtok": entry.output_per_mtok, "price_source": entry.price_source,
            })
            if entry.endpoint_name and entry.endpoint_name not in row["endpoints"]:
                row["endpoints"].append(entry.endpoint_name)
        return sorted(rows.values(), key=lambda r: (r["input_per_mtok"] is not None, r["model"].lower()))

    @router.get("/prices")
    async def get_prices(request: Request):
        """Every metered model you can use, with the price the budget bills it at."""
        owner = _owner(request)
        return {"models": await asyncio.to_thread(_price_rows, owner), "can_edit": _is_admin(request)}

    @router.put("/prices")
    async def put_price(request: Request, body: PriceIn):
        """Declare a model's list price (admin): it then prices the budget, the
        pickers and the Council estimate for every endpoint serving that model."""
        from core.middleware import require_admin
        from src.omnigent_catalog import save_declared_price

        require_admin(request)
        entry = await asyncio.to_thread(save_declared_price, body.model.strip(), body.input_per_mtok,
                                        body.output_per_mtok)
        budget.clear_price_cache()
        return {"model": body.model.strip(), **entry}

    return router


def _is_admin(request: Request) -> bool:
    from core.middleware import require_admin

    try:
        require_admin(request)
        return True
    except HTTPException:
        return False
