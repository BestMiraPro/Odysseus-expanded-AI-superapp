"""Omnigent integration routes for Odysseus."""

from __future__ import annotations

import json
import os
import tarfile
from io import BytesIO
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request, Response

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src.auth_helpers import get_current_user, owner_filter, require_authenticated_request
from src.constants import DATA_DIR
from src.omnigent_native import NativeOmnigentManager
from src.omnigent_manager import INSTALL_GUIDANCE, OmnigentManager


# Prefer a reasoning-capable general model as the default when installing API
# gateways into the bundled Omnigent.
_DEFAULT_MODEL_PREFS = ("glm-5", "glm", "qwen3", "deepseek", "llama")


def _endpoint_model_ids(ep) -> list[str]:
    ids: list[str] = []
    for raw in (ep.pinned_models, ep.cached_models):
        try:
            for m in json.loads(raw or "[]"):
                mid = m.get("id") if isinstance(m, dict) else m
                if mid and mid not in ids:
                    ids.append(mid)
        except Exception:
            pass
    return ids


def _provider_slug(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in (name or "api").lower()).strip("-")
    return slug or "api"


def _pick_default_model(ids: list[str]) -> str | None:
    for pref in _DEFAULT_MODEL_PREFS:
        for mid in ids:
            if pref in mid.lower():
                return mid
    return ids[0] if ids else None


def _install_api_models(user: str | None) -> dict:
    """Write every enabled Odysseus API endpoint into the bundled Omnigent as an
    OpenAI-compatible ``gateway`` provider (so all their models are available and
    marked API the moment the server boots).

    Keys are written inline into a 0600 config file: Omnigent's ``env:`` refs
    don't thread down to the harness that resolves the credential, so a file the
    harness reads directly is the reliable path. Lives in the persisted
    ``omnigent-home`` so it survives container recreates.
    """
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if user:
            q = owner_filter(q, ModelEndpoint, user)
        endpoints = [
            ep for ep in q.all()
            if (ep.model_type or "llm") == "llm" and ep.base_url and ep.api_key
        ]
        providers: dict[str, dict] = {}
        default_model: str | None = None
        model_count = 0
        used: set[str] = set()
        for idx, ep in enumerate(endpoints):
            slug = _provider_slug(ep.name or ep.base_url)
            while slug in used:
                slug += "-x"
            used.add(slug)
            ids = _endpoint_model_ids(ep)
            model_count += len(ids)
            pick = _pick_default_model(ids)
            if default_model is None and pick:
                default_model = pick
            family = {"base_url": (ep.base_url or "").rstrip("/"), "api_key": ep.api_key, "wire_api": "chat"}
            if pick:
                family["models"] = {"default": pick}
            block: dict = {"kind": "gateway", "openai": family}
            if idx == 0:
                block["default"] = ["openai"]
            providers[slug] = block
    finally:
        db.close()
    if not providers:
        return {"endpoints": 0, "models": 0, "default_model": None}
    cfg_dir = Path(DATA_DIR) / "omnigent-home" / ".omnigent"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    cfg["providers"] = providers
    cfg["harness"] = "openai-agents"
    if default_model:
        cfg["model"] = default_model
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    try:
        os.chmod(cfg_path, 0o600)
    except Exception:
        pass
    return {"endpoints": len(providers), "models": model_count, "default_model": default_model}


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

    @router.post("/launch")
    def launch(request: Request):
        # Install the owner's enabled API model endpoints into the bundled
        # Omnigent (as OpenAI-compatible gateway providers, marked API), then
        # boot its server (which serves Omnigent's own chat web UI). Degrades
        # gracefully when the Omnigent CLI isn't installed where Odysseus runs.
        require_authenticated_request(request)
        user = get_current_user(request)
        try:
            api = _install_api_models(user)
        except Exception as exc:
            api = {"endpoints": 0, "models": 0, "error": str(exc)}
        try:
            data = manager.start()
        except Exception as exc:
            data = manager.status()
            data["error"] = str(exc)
        data["install"] = INSTALL_GUIDANCE
        data["api_models"] = api
        return data

    return router
