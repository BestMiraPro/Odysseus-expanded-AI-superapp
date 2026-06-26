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
from src.omnigent_agents import OmnigentAgentStore, compile_config
from src.constants import DATA_DIR


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
    agent_store: OmnigentAgentStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/omnigent", tags=["omnigent"])
    manager = manager or OmnigentManager()
    native_manager = native_manager or NativeOmnigentManager()
    agent_store = agent_store or OmnigentAgentStore()

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
        # Conversational sessions live in the external Omnigent server.
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

    @router.get("/bundle.tar.gz")
    def bundle(request: Request):
        require_authenticated_request(request)
        root = Path(__file__).resolve().parent.parent / "integrations" / "omnigent"
        headers = {"Content-Disposition": 'attachment; filename="odysseus-omnigent-bundle.tar.gz"'}
        return Response(content=_bundle_bytes(root), media_type="application/gzip", headers=headers)

    # ---- custom agents (owner-scoped) -----------------------------------
    @router.get("/agents")
    def list_agents(request: Request):
        user = require_authenticated_request(request)
        return {"agents": agent_store.list_agents(user)}

    @router.post("/agents")
    async def create_agent(request: Request):
        user = require_authenticated_request(request)
        data = await request.json()
        try:
            return agent_store.create_agent(
                owner=user,
                name=data.get("name", ""),
                role=data.get("role", ""),
                backend=data.get("backend", ""),
                model=data.get("model"),
                enabled=data.get("enabled", True),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.put("/agents/{agent_id}")
    async def update_agent(request: Request, agent_id: str):
        user = require_authenticated_request(request)
        data = await request.json()
        try:
            agent = agent_store.update_agent(agent_id, owner=user, **data)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent

    @router.delete("/agents/{agent_id}")
    def delete_agent(request: Request, agent_id: str):
        user = require_authenticated_request(request)
        if not agent_store.delete_agent(agent_id, owner=user):
            raise HTTPException(404, "Agent not found")
        return {"ok": True}

    # ---- orchestrator + compile + model options -------------------------
    @router.get("/orchestrator")
    def get_orchestrator(request: Request):
        user = require_authenticated_request(request)
        return agent_store.get_orchestrator(user)

    @router.put("/orchestrator")
    async def set_orchestrator(request: Request):
        user = require_authenticated_request(request)
        data = await request.json()
        try:
            return agent_store.set_orchestrator(
                user,
                backend=data.get("backend", ""),
                model=data.get("model"),
                workspace=data.get("workspace", ""),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/agents/compile")
    def compile_agents(request: Request):
        user = require_authenticated_request(request)
        text = compile_config(agent_store.list_agents(user), agent_store.get_orchestrator(user))
        return {"yaml": text, "filename": "config.yaml"}

    @router.get("/model-options")
    def model_options(request: Request):
        import json as _json
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if user:
                q = owner_filter(q, ModelEndpoint, user)
            out = []
            for ep in q.all():
                if (ep.model_type or "llm") != "llm":
                    continue
                models = []
                for raw in (ep.cached_models, ep.pinned_models):
                    try:
                        models += _json.loads(raw or "[]")
                    except Exception:
                        pass
                seen, uniq = set(), []
                for m in models:
                    mid = m.get("id") if isinstance(m, dict) else m
                    if mid and mid not in seen:
                        seen.add(mid)
                        uniq.append(mid)
                out.append({"id": ep.id, "name": ep.name, "models": uniq})
            return {"endpoints": out}
        finally:
            db.close()

    @router.post("/launch")
    def launch(request: Request):
        # One-click: compile + save the crew config, then boot the local
        # Omnigent server (which serves Omnigent's own chat web UI). Degrades
        # gracefully when the Omnigent CLI isn't installed where Odysseus runs.
        user = require_authenticated_request(request)
        text = compile_config(agent_store.list_agents(user), agent_store.get_orchestrator(user))
        cfg_dir = Path(DATA_DIR) / "omnigent"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / f"{(user or 'crew')}.yaml"
        cfg_path.write_text(text, encoding="utf-8")
        try:
            data = manager.start()
        except Exception as exc:
            data = manager.status()
            data["error"] = str(exc)
        data["config_path"] = str(cfg_path)
        data["install"] = INSTALL_GUIDANCE
        return data

    return router
