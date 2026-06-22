"""Omnigent integration routes for Odysseus."""

from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src.auth_helpers import get_current_user, owner_filter, require_authenticated_request
from src.omnigent_native import NativeOmnigentManager
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


def _has_visible_model_endpoint(request: Request | None = None) -> bool:
    user = get_current_user(request) if request is not None else None
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if user:
            q = owner_filter(q, ModelEndpoint, user)
        return q.first() is not None
    except Exception:
        return False
    finally:
        db.close()


def setup_omnigent_routes(
    manager: OmnigentManager | None = None,
    native_manager: NativeOmnigentManager | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/omnigent", tags=["omnigent"])
    manager = manager or OmnigentManager()
    native_manager = native_manager or NativeOmnigentManager()

    @router.get("/status")
    def status(request: Request):
        data = manager.status()
        data["install"] = INSTALL_GUIDANCE
        data["native"] = native_manager.status(model_ready=_has_visible_model_endpoint(request))
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
    def sessions(request: Request):
        user = get_current_user(request)
        native_sessions = native_manager.sessions(user)
        if native_sessions.get("sessions"):
            external = manager.sessions()
            native_sessions["external_sessions"] = external.get("sessions", [])
            native_sessions["running"] = external.get("running")
            native_sessions["url"] = external.get("url")
            return native_sessions
        return manager.sessions()

    @router.get("/workers")
    def workers():
        data = manager.workers()
        data["native_workers"] = native_manager.worker_roster()
        data["presets"] = list(native_manager.presets().values())
        return data

    @router.get("/providers")
    def providers():
        return {"providers": _providers()}

    @router.get("/capabilities")
    def capabilities(request: Request):
        token_scopes = sorted(set(getattr(request.state, "api_token_scopes", []) or []))
        return {
            "integration": "omnigent",
            "native": native_manager.status(model_ready=_has_visible_model_endpoint(request)),
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

    @router.post("/runs")
    async def create_run(request: Request):
        require_authenticated_request(request)
        user = get_current_user(request)
        data = await request.json()
        try:
            return native_manager.create_run(
                owner=user,
                goal=data.get("goal", ""),
                preset=data.get("preset", "balanced"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/runs/{run_id}")
    def get_run(request: Request, run_id: str):
        require_authenticated_request(request)
        user = get_current_user(request)
        run = native_manager.get_run(run_id, owner=user)
        if not run:
            raise HTTPException(404, "Crew run not found")
        return run

    @router.post("/runs/{run_id}/start")
    def start_run(request: Request, run_id: str):
        require_authenticated_request(request)
        user = get_current_user(request)
        run = native_manager.start_run(run_id, owner=user)
        if not run:
            raise HTTPException(404, "Crew run not found")
        return run

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(request: Request, run_id: str):
        require_authenticated_request(request)
        user = get_current_user(request)
        run = native_manager.cancel_run(run_id, owner=user)
        if not run:
            raise HTTPException(404, "Crew run not found")
        return run

    @router.get("/bundle.tar.gz")
    def bundle(request: Request):
        require_authenticated_request(request)
        root = Path(__file__).resolve().parent.parent / "integrations" / "omnigent"
        headers = {"Content-Disposition": 'attachment; filename="odysseus-omnigent-bundle.tar.gz"'}
        return Response(content=_bundle_bytes(root), media_type="application/gzip", headers=headers)

    return router
