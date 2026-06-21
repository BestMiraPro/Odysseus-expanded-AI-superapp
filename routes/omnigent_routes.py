"""Omnigent integration routes for Odysseus."""

from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from core.middleware import require_admin
from src.auth_helpers import require_authenticated_request
from src.omnigent_manager import INSTALL_GUIDANCE, OmnigentManager


def _providers() -> list[dict]:
    return [
        {
            "id": "chatgpt-subscription",
            "label": "ChatGPT Subscription",
            "kind": "account",
            "supported": True,
            "auth_flow": "chatgpt-subscription",
            "description": "Use the ChatGPT account already linked through Odysseus, alongside scoped Odysseus tools.",
        },
        {
            "id": "claude-subscription",
            "label": "Claude Subscription",
            "kind": "cli",
            "supported": True,
            "description": "Use a Claude subscription configured in the local Claude/Omnigent environment.",
            "setup_hint": "Run omnigent setup or your Claude CLI login, then choose the Claude harness in Omnigent.",
        },
        {
            "id": "api-endpoint",
            "label": "API endpoint",
            "kind": "api",
            "supported": True,
            "description": "Use any paid or local API endpoint already configured in Odysseus, including OpenAI-compatible providers.",
        },
        {
            "id": "glm-free-web",
            "label": "GLM website",
            "kind": "web",
            "supported": False,
            "url": "https://chat.z.ai/",
            "note": "GLM free website access is not an API connector. Odysseus already has existing Z.AI API endpoints for API and Coding Plan use, so no paid GLM duplicate is added here.",
        },
    ]


def _bundle_bytes(root: Path) -> bytes:
    if not root.exists():
        raise HTTPException(404, "Omnigent bundle not found")
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(root.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            tf.add(path, arcname=str(path.relative_to(root)).replace("\\", "/"))
    return buf.getvalue()


def setup_omnigent_routes(manager: OmnigentManager | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/omnigent", tags=["omnigent"])
    manager = manager or OmnigentManager()

    @router.get("/status")
    def status():
        data = manager.status()
        data["install"] = INSTALL_GUIDANCE
        return data

    @router.post("/server/start")
    def start_server(request: Request):
        require_admin(request)
        try:
            data = manager.start()
        except Exception as exc:
            raise HTTPException(500, str(exc))
        data["install"] = INSTALL_GUIDANCE
        return data

    @router.post("/server/stop")
    def stop_server(request: Request):
        require_admin(request)
        try:
            data = manager.stop()
        except Exception as exc:
            raise HTTPException(500, str(exc))
        data["install"] = INSTALL_GUIDANCE
        return data

    @router.get("/sessions")
    def sessions():
        return manager.sessions()

    @router.get("/workers")
    def workers():
        return manager.workers()

    @router.get("/providers")
    def providers():
        return {"providers": _providers()}

    @router.get("/capabilities")
    def capabilities(request: Request):
        token_scopes = sorted(set(getattr(request.state, "api_token_scopes", []) or []))
        return {
            "integration": "omnigent",
            "token_scopes": token_scopes,
            "providers": _providers(),
            "bridge": {
                "bundle": "/api/omnigent/bundle.tar.gz",
                "uses": "/api/codex/*",
                "required_env": ["ODYSSEUS_URL", "ODYSSEUS_API_TOKEN"],
            },
            "tools": [
                "odysseus_capabilities",
                "odysseus_todos",
                "odysseus_email_search",
                "odysseus_memory",
                "odysseus_calendar_events",
                "odysseus_documents",
                "odysseus_cookbook_tasks",
            ],
        }

    @router.get("/bundle.tar.gz")
    def bundle(request: Request):
        require_authenticated_request(request)
        root = Path(__file__).resolve().parent.parent / "integrations" / "omnigent"
        headers = {"Content-Disposition": 'attachment; filename="odysseus-omnigent-bundle.tar.gz"'}
        return Response(content=_bundle_bytes(root), media_type="application/gzip", headers=headers)

    return router
