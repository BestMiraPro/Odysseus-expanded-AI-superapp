"""Claude Subscription setup routes.

Connects a Claude Pro/Max subscription to Odysseus through the Claude Code CLI
(see src/claude_subscription.py). Once connected it is an ordinary owner-scoped
model endpoint, usable from chat, Study, Council and every other model picker.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import claude_subscription
from src.auth_helpers import get_current_user, require_user

logger = logging.getLogger(__name__)


class ConnectRequest(BaseModel):
    mode: str = claude_subscription.AUTH_MODE_TOKEN
    token: Optional[str] = None
    verify: bool = True


def _is_admin(request: Request) -> bool:
    try:
        require_admin(request)
        return True
    except HTTPException:
        return False


def status_payload(request: Request) -> dict:
    owner = get_current_user(request) or None
    cli_path = claude_subscription.find_cli()
    data = {
        "provider": claude_subscription.CLAUDE_SUBSCRIPTION_PROVIDER,
        "label": claude_subscription.CLAUDE_SUBSCRIPTION_LABEL,
        "cli_installed": bool(cli_path),
        "connection": claude_subscription.connection(owner),
        "models": claude_subscription.default_models(),
        "setup": {
            "install": "npm install -g @anthropic-ai/claude-code",
            "token_command": "claude setup-token",
            "host_login_command": "claude auth login",
        },
    }
    if cli_path and _is_admin(request):
        # Host login state is a property of the server machine, so only admins
        # (who may connect it) see it.
        data["cli_version"] = claude_subscription.cli_version()
        data["host_login"] = claude_subscription.host_login_status()
    return data


def setup_claude_subscription_routes() -> APIRouter:
    router = APIRouter(prefix="/api/claude-subscription", tags=["claude-subscription"])

    @router.get("/status")
    def status(request: Request):
        require_user(request)
        return status_payload(request)

    @router.post("/connect")
    async def connect(request: Request, body: ConnectRequest):
        require_admin(request)
        owner = get_current_user(request) or None
        mode = (body.mode or "").strip().lower()
        token = (body.token or "").strip() or None
        if mode not in (claude_subscription.AUTH_MODE_TOKEN, claude_subscription.AUTH_MODE_HOST):
            raise HTTPException(400, "mode must be 'token' or 'host'")
        if mode == claude_subscription.AUTH_MODE_TOKEN and not claude_subscription.valid_token(token):
            raise HTTPException(400, "Paste the token printed by `claude setup-token`.")
        if mode == claude_subscription.AUTH_MODE_HOST:
            token = None
        if not claude_subscription.find_cli():
            raise HTTPException(
                503, "The Claude Code CLI is not installed where Odysseus runs. "
                     "Install it with: npm install -g @anthropic-ai/claude-code")
        if body.verify:
            try:
                await claude_subscription.verify(mode, token)
            except claude_subscription.ClaudeSubscriptionError as exc:
                raise HTTPException(exc.status if exc.status >= 400 else 502, str(exc))
        try:
            result = claude_subscription.provision(owner, mode, token)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            logger.warning("claude subscription provisioning failed error_type=%s", type(exc).__name__)
            raise HTTPException(500, "Could not save the Claude Subscription connection")
        return {"status": "connected", "endpoint": result}

    @router.post("/disconnect")
    def disconnect(request: Request):
        require_admin(request)
        owner = get_current_user(request) or None
        return {"status": "disconnected", **claude_subscription.disconnect(owner)}

    return router
