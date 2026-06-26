"""Omnigent route and bundle tests."""

from __future__ import annotations

import asyncio
import tarfile
from io import BytesIO
from types import SimpleNamespace


def _handler(router, method: str, path: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} not found")


class _FakeManager:
    def __init__(self):
        self.started = False
        self.stopped = False

    def status(self):
        return {
            "installed": True,
            "command": "omnigent",
            "version": "omnigent 0.2.0",
            "running": False,
            "url": None,
            "sessions": None,
            "log_path": None,
        }

    def start(self):
        self.started = True
        return {
            "installed": True,
            "command": "omnigent",
            "running": True,
            "url": "http://127.0.0.1:6767",
            "last_command": "omnigent server start",
        }

    def stop(self):
        self.stopped = True
        return {
            "installed": True,
            "command": "omnigent",
            "running": False,
            "url": None,
            "last_command": "omnigent server stop",
        }

    def sessions(self):
        return {"sessions": [{"id": "conv_1", "title": "Plan launch", "status": "idle"}]}

    def workers(self):
        return {
            "recommended_prompt": "start claude and codex",
            "workers": [
                {
                    "id": "claude_code",
                    "label": "Claude",
                    "status": "ready",
                    "available": True,
                    "source": "subscription",
                },
                {
                    "id": "codex",
                    "label": "Codex",
                    "status": "ready",
                    "available": True,
                    "source": "subscription",
                },
            ],
        }


class _JsonRequest(SimpleNamespace):
    def __init__(self, payload=None):
        super().__init__(state=SimpleNamespace())
        self._payload = payload or {}

    async def json(self):
        return self._payload


def test_status_route_includes_install_guidance():
    from routes.omnigent_routes import setup_omnigent_routes

    status = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/status")(_JsonRequest())

    assert status["installed"] is True
    assert status["command"] == "omnigent"
    assert "install_oss.sh" in status["install"]["recommended"]
    assert "uv tool install omnigent" in status["install"]["alternatives"]


def test_start_and_stop_routes_delegate_to_manager(monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import setup_omnigent_routes

    monkeypatch.setattr(omnigent_routes, "require_admin", lambda request: None)
    manager = _FakeManager()
    router = setup_omnigent_routes(manager)

    started = _handler(router, "POST", "/api/omnigent/server/start")(SimpleNamespace())
    stopped = _handler(router, "POST", "/api/omnigent/server/stop")(SimpleNamespace())

    assert manager.started is True
    assert manager.stopped is True
    assert started["running"] is True
    assert stopped["running"] is False


def test_providers_skip_copilot_and_do_not_duplicate_paid_glm_api():
    from routes.omnigent_routes import setup_omnigent_routes

    providers = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/providers")()["providers"]
    provider_ids = {p["id"] for p in providers}

    assert "chatgpt-subscription" in provider_ids
    assert "claude-subscription" in provider_ids
    assert "api-endpoint" in provider_ids
    assert "glm-free-web" in provider_ids
    assert "copilot" not in provider_ids
    assert "glm-coding-plan" not in provider_ids

    glm = next(p for p in providers if p["id"] == "glm-free-web")
    assert glm["supported"] is False
    assert "free website" in glm["note"].lower()
    assert "existing Z.AI API endpoints" in glm["note"]


def test_bundle_route_serves_omnigent_agent_tarball(monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import setup_omnigent_routes

    monkeypatch.setattr(omnigent_routes, "require_authenticated_request", lambda request: None)
    response = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/bundle.tar.gz")(
        SimpleNamespace()
    )

    assert response.media_type == "application/gzip"
    assert "odysseus-omnigent-bundle.tar.gz" in response.headers["Content-Disposition"]

    with tarfile.open(fileobj=BytesIO(response.body), mode="r:gz") as tf:
        names = set(tf.getnames())
        assert "config.yaml" in names
        assert "tools/odysseus_api.py" in names

        config = tf.extractfile("config.yaml").read().decode("utf-8")
        script = tf.extractfile("tools/odysseus_api.py").read().decode("utf-8")

    assert "name: odysseus" in config
    assert "ODYSSEUS_URL" in config
    assert "ODYSSEUS_API_TOKEN" in config
    assert "/api/codex/capabilities" in script
    assert "Bearer {token}" in script


def test_sessions_route_proxies_manager_sessions():
    from routes.omnigent_routes import setup_omnigent_routes

    result = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/sessions")(_JsonRequest())

    assert result["sessions"][0]["id"] == "conv_1"


def test_workers_route_reports_original_omnigent_roster_shape():
    from routes.omnigent_routes import setup_omnigent_routes

    result = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/workers")()

    assert result["recommended_prompt"] == "start claude and codex"
    assert [worker["id"] for worker in result["workers"]] == ["claude_code", "codex"]
    assert all("status" in worker for worker in result["workers"])


def test_status_route_reports_native_mode(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import setup_omnigent_routes
    from src.omnigent_native import NativeOmnigentManager

    monkeypatch.setattr(omnigent_routes, "_has_visible_model_endpoint", lambda request=None: True)
    native = NativeOmnigentManager(state_path=tmp_path / "runs.json")

    status = _handler(setup_omnigent_routes(_FakeManager(), native), "GET", "/api/omnigent/status")(_JsonRequest())

    assert status["native"]["available"] is True
    assert status["native"]["mode"] == "native"
    assert status["native"]["model_ready"] is True


def test_workers_route_includes_native_roster(tmp_path):
    from routes.omnigent_routes import setup_omnigent_routes
    from src.omnigent_native import NativeOmnigentManager

    native = NativeOmnigentManager(state_path=tmp_path / "runs.json")

    result = _handler(setup_omnigent_routes(_FakeManager(), native), "GET", "/api/omnigent/workers")()

    native_ids = {worker["id"] for worker in result["native_workers"]}
    assert {"architect", "researcher", "coder", "reviewer", "executor"} <= native_ids
    assert result["presets"][0]["id"] == "balanced"


def test_sessions_route_uses_external_omnigent_only():
    from routes.omnigent_routes import setup_omnigent_routes

    result = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/sessions")(_JsonRequest())

    # native goal-run sessions were removed; only the external server is proxied
    assert result["sessions"][0]["id"] == "conv_1"


def test_native_run_routes_are_removed():
    from routes.omnigent_routes import setup_omnigent_routes

    paths = {getattr(r, "path", "") for r in setup_omnigent_routes(_FakeManager()).routes}
    assert "/api/omnigent/runs" not in paths
    assert "/api/omnigent/runs/{run_id}/start" not in paths
    assert "/api/omnigent/runs/{run_id}/cancel" not in paths


def test_agents_crud_routes_owner_scoped(tmp_path, monkeypatch):
    import asyncio
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import setup_omnigent_routes
    from src.omnigent_agents import OmnigentAgentStore

    monkeypatch.setattr(omnigent_routes, "require_authenticated_request", lambda request: "alice")
    monkeypatch.setattr(omnigent_routes, "get_current_user", lambda request: "alice")
    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    router = setup_omnigent_routes(_FakeManager(), agent_store=store)

    created = asyncio.run(_handler(router, "POST", "/api/omnigent/agents")(
        _JsonRequest({"name": "Researcher", "role": "Find context.", "backend": "claude-subscription"})
    ))
    listed = _handler(router, "GET", "/api/omnigent/agents")(_JsonRequest())
    updated = asyncio.run(_handler(router, "PUT", "/api/omnigent/agents/{agent_id}")(
        _JsonRequest({"enabled": False}), created["id"]
    ))
    deleted = _handler(router, "DELETE", "/api/omnigent/agents/{agent_id}")(_JsonRequest(), created["id"])

    assert created["name"] == "Researcher"
    assert listed["agents"][0]["id"] == created["id"]
    assert updated["enabled"] is False
    assert deleted["ok"] is True


def test_orchestrator_and_compile_routes(tmp_path, monkeypatch):
    import asyncio
    import yaml
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import setup_omnigent_routes
    from src.omnigent_agents import OmnigentAgentStore

    monkeypatch.setattr(omnigent_routes, "require_authenticated_request", lambda request: "alice")
    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    store.create_agent(owner="alice", name="Coder", role="Build.", backend="claude-subscription")
    router = setup_omnigent_routes(_FakeManager(), agent_store=store)

    got = _handler(router, "GET", "/api/omnigent/orchestrator")(_JsonRequest())
    saved = asyncio.run(_handler(router, "PUT", "/api/omnigent/orchestrator")(
        _JsonRequest({"backend": "chatgpt-subscription"})
    ))
    compiled = _handler(router, "GET", "/api/omnigent/agents/compile")(_JsonRequest())

    assert got["backend"] == "claude-subscription"   # default
    assert saved["backend"] == "chatgpt-subscription"
    spec = yaml.safe_load(compiled["yaml"])
    assert spec["executor"]["config"]["harness"] == "codex"
    assert spec["agents"][0]["name"] == "Coder"
