"""Omnigent route and bundle tests."""

from __future__ import annotations

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


def test_status_route_includes_install_guidance():
    from routes.omnigent_routes import setup_omnigent_routes

    status = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/status")()

    assert status["installed"] is True
    assert status["command"] == "omnigent"
    assert status["install"]["recommended"] == "uv tool install omnigent"
    assert "pip install omnigent" in status["install"]["alternatives"]


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

    result = _handler(setup_omnigent_routes(_FakeManager()), "GET", "/api/omnigent/sessions")()

    assert result["sessions"][0]["id"] == "conv_1"
