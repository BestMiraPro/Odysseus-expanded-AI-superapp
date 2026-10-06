"""Omnigent route and bundle tests."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


def _handler(router, method: str, path: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} not found")


class _FakeManager:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.start_env_extra = None

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

    def start(self, env_extra=None):
        self.started = True
        self.start_env_extra = env_extra
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
    monkeypatch.setattr(omnigent_routes, "_install_api_models", lambda user: {"endpoints": 0, "models": 0})
    monkeypatch.setattr(
        omnigent_routes,
        "_builtin_agent_env",
        lambda: {"OMNIGENT_BUILTIN_AGENT_DIRS": f"crew{os.pathsep}crew-glm-5-2"},
    )
    monkeypatch.setattr(
        omnigent_routes,
        "_gateway_credentials_env",
        lambda user: {"OPENAI_API_KEY": "key", "OPENAI_BASE_URL": "https://api.example/v1"},
    )
    monkeypatch.setattr(omnigent_routes, "_refresh_generated_session_agent_rows", lambda: 0)
    manager = _FakeManager()
    router = setup_omnigent_routes(manager)

    started = _handler(router, "POST", "/api/omnigent/server/start")(_JsonRequest())
    stopped = _handler(router, "POST", "/api/omnigent/server/stop")(SimpleNamespace())

    assert manager.started is True
    assert manager.stopped is True
    assert manager.start_env_extra["OMNIGENT_BUILTIN_AGENT_DIRS"] == f"crew{os.pathsep}crew-glm-5-2"
    assert manager.start_env_extra["OPENAI_API_KEY"] == "key"
    assert manager.start_env_extra["OPENAI_BASE_URL"] == "https://api.example/v1"
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


def test_agent_config_routes_are_removed():
    from routes.omnigent_routes import setup_omnigent_routes

    paths = {getattr(r, "path", "") for r in setup_omnigent_routes(_FakeManager()).routes}
    for gone in ("/api/omnigent/agents", "/api/omnigent/agents/compile", "/api/omnigent/model-options"):
        assert gone not in paths, f"{gone} should be removed"
    # the launcher endpoints stay, and the universal crew's orchestrator is choosable
    assert "/api/omnigent/launch" in paths
    assert "/api/omnigent/status" in paths
    assert "/api/omnigent/orchestrator" in paths
    assert "/api/omnigent/server/restart" in paths


def test_install_helpers_shape_api_gateways():
    from routes.omnigent_routes import _provider_slug, _pick_default_model, _endpoint_model_ids

    assert _provider_slug("api.inference.wandb.ai") == "api-inference-wandb-ai"
    assert _provider_slug("") == "api"
    # a reasoning-capable general model is preferred as the default
    assert _pick_default_model(["Qwen/Qwen3-235B", "zai-org/GLM-5.2", "x"]) == "zai-org/GLM-5.2"
    assert _pick_default_model(["only/thing"]) == "only/thing"
    assert _pick_default_model([]) is None

    class _Ep:
        pinned_models = '[{"id": "a"}, {"id": "b"}]'
        cached_models = '["b", "c"]'

    assert _endpoint_model_ids(_Ep()) == ["a", "b", "c"]


def test_credentials_env_for_imports_gateway_key_for_spawned_workers():
    from routes.omnigent_routes import _credentials_env_for

    env = _credentials_env_for("https://api.inference.wandb.ai/v1/", "wandb_key")
    # spawned openai-agents workers read ambient OPENAI_* + the harness vars
    assert env["OPENAI_BASE_URL"] == "https://api.inference.wandb.ai/v1"  # trailing slash trimmed
    assert env["OPENAI_API_KEY"] == "wandb_key"
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"] == "https://api.inference.wandb.ai/v1"
    assert env["HARNESS_OPENAI_AGENTS_API_KEY"] == "wandb_key"
    # W&B is chat-only — force Chat Completions so the Responses API doesn't 404
    assert env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] == "false"
    # no usable creds -> no env (nothing partial leaks)
    assert _credentials_env_for("", "wandb_key") is None
    assert _credentials_env_for("https://x", "") is None


def test_model_slug_for_worker_dirs():
    from routes.omnigent_routes import _model_slug, _generate_crew

    # worker dir names derive from the model id's tail, lowercased + dash-safe
    assert _model_slug("zai-org/GLM-5.2") == "glm-5-2"
    assert _model_slug("deepseek-ai/DeepSeek-V3.1") == "deepseek-v3-1"
    assert _model_slug("") == "model"


def test_executor_block_bakes_inline_api_key_auth():
    from routes.omnigent_routes import _executor_block

    # the model id must sit at executor.model (not config.model)
    block = _executor_block("zai-org/GLM-5.2", ("https://api.inference.wandb.ai/v1/", "wandb_key"))
    assert block["model"] == "zai-org/GLM-5.2"
    assert block["config"]["harness"] == "openai-agents"
    # chat-only gateways 404 the Responses API, so force Chat Completions.
    # Omnigent str()-coerces config scalars, so only "" stays falsy and yields
    # HARNESS_OPENAI_AGENTS_USE_RESPONSES=false (a bool would become "False").
    assert block["config"]["use_responses"] == ""
    # creds are baked inline so they resolve regardless of the daemon's HOME
    assert block["auth"] == {
        "type": "api_key",
        "api_key": "wandb_key",
        "base_url": "https://api.inference.wandb.ai/v1",  # trailing slash trimmed
    }
    # no creds -> no auth key (falls back to the providers: gateway path)
    assert "auth" not in _executor_block("x/y", None)
    assert "auth" not in _executor_block("x/y", ("https://x", ""))




# ---------------------------------------------------------------------------
# Universal crew
# ---------------------------------------------------------------------------

def _crew(tmp_path):
    return yaml.safe_load((tmp_path / "omnigent-home" / ".omnigent" / "agents" / "crew" / "config.yaml")
                          .read_text(encoding="utf-8"))


def _isolate(monkeypatch, tmp_path):
    import routes.omnigent_routes as omnigent_routes
    from src import model_roster

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(model_roster.PRICES, "lookup", lambda model_id: None)
    return omnigent_routes


def test_universal_crew_with_no_api_models_still_has_native_workers(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    assert og._generate_crew({}, None) == 2
    crew = _crew(tmp_path)
    assert crew["name"] == "crew"
    assert crew["tools"]["agents"] == ["claude-code", "codex"]
    assert crew["executor"]["config"]["harness"] == "claude-sdk"


def test_universal_crew_rosters_every_model_with_recommendation_and_cost(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    creds = {m: ("https://api.example/v1", "k") for m in ("glm-4.6", "zai-org/GLM-5.2", "deepseek-v4-flash")}
    meta = {m: {"endpoint_id": "e1", "endpoint_name": "Gateway", "kind": "api", "provider": "openai"} for m in creds}

    assert og._generate_crew(creds, None, model_meta=meta) == 5

    crew = _crew(tmp_path)
    assert crew["tools"]["agents"][:2] == ["claude-code", "codex"]
    # recommended (newest of family) ranks ahead of the older GLM
    assert crew["tools"]["agents"].index("glm-5-2") < crew["tools"]["agents"].index("glm-4-6")
    prompt = crew["prompt"]
    assert "`glm-5-2`: zai-org/GLM-5.2 via Gateway — RECOMMENDED" in prompt
    assert "`glm-4-6`: glm-4.6 via Gateway — api" in prompt
    assert "price unknown" in prompt
    assert "Choosing a model" in prompt and "TOOL-CALL PROTOCOL" in prompt
    worker = yaml.safe_load((tmp_path / "omnigent-home" / ".omnigent" / "agents" / "crew" / "agents" / "glm-5-2"
                             / "config.yaml").read_text(encoding="utf-8"))
    assert worker["executor"]["model"] == "zai-org/GLM-5.2"
    assert worker["description"] == "zai-org/GLM-5.2 worker (via Gateway)."


def test_api_orchestrator_leads_and_is_not_its_own_worker(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    creds = {"zai-org/GLM-5.2": ("https://api.example/v1", "k"), "deepseek-v4-flash": ("https://api.example/v1", "k")}

    og._generate_crew(creds, "api::e1::zai-org/GLM-5.2")

    crew = _crew(tmp_path)
    assert crew["executor"]["model"] == "zai-org/GLM-5.2"
    assert crew["executor"]["config"]["harness"] == "openai-agents"
    assert "glm-5-2" not in crew["tools"]["agents"]
    assert "deepseek-v4-flash" in crew["tools"]["agents"]
    assert "running on zai-org/GLM-5.2 (API)" in crew["prompt"]


def test_codex_orchestrator_uses_saved_reasoning_effort(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    og.save_crew_settings({"orchestrator": "codex::gpt-5.5", "reasoning_effort": "xhigh", "max_workers": 40})

    og._generate_crew({})

    crew = _crew(tmp_path)
    assert crew["executor"]["model"] == "gpt-5.5"
    assert crew["executor"]["config"] == {"harness": "codex-native", "yolo": True, "reasoning_effort": "xhigh"}
    assert crew["llm"] == {"model": "gpt-5.5", "reasoning_effort": "xhigh"}


def test_unknown_or_vanished_api_orchestrator_falls_back_to_claude(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    og._generate_crew({}, "api::gone::some-model")
    assert _crew(tmp_path)["executor"]["config"]["harness"] == "claude-sdk"
    assert og.parse_orchestrator_id("nonsense") == {"kind": "claude", "model": None, "endpoint_id": None}
    assert og.parse_orchestrator_id("claude::claude-opus-5-5")["model"] == "claude-opus-5-5"


def test_max_workers_caps_the_api_roster(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    og.save_crew_settings({"orchestrator": "claude", "reasoning_effort": "high", "max_workers": 1})
    creds = {f"model-{i}": ("https://api.example/v1", "k") for i in range(5)}
    assert og._generate_crew(creds) == 3          # claude-code + codex + 1 API worker


def test_regeneration_retires_old_generated_crews_but_keeps_user_crews(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    agents_root = tmp_path / "omnigent-home" / ".omnigent" / "agents"
    for name, desc in (("crew-codex", "anything"),
                       ("crew-glm-5-2", "zai-org/GLM-5.2-brained crew running directly on zai-org/GLM-5.2"),
                       ("crew-research", "My own research crew")):
        (agents_root / name).mkdir(parents=True)
        (agents_root / name / "config.yaml").write_text(yaml.safe_dump({"name": name, "description": desc}))

    og._generate_crew({})

    assert not (agents_root / "crew-codex").exists()
    assert not (agents_root / "crew-glm-5-2").exists()
    assert (agents_root / "crew-research" / "config.yaml").exists()
    assert og._retired_agent_names(agents_root) == {"crew-codex", "crew-glm-5-2"}
    env = og._builtin_agent_env()
    assert [Path(p).name for p in env["OMNIGENT_BUILTIN_AGENT_DIRS"].split(os.pathsep)] == ["crew"]


def test_purge_removes_universal_retired_and_worker_template_rows(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    og._generate_crew({"zai-org/GLM-5.2": ("https://api.example/v1", "k")})
    root = tmp_path / "omnigent-home" / ".omnigent"
    manifest = json.loads((root / ".odysseus-generated.json").read_text())
    manifest["retired"] = ["crew-glm-5-2", "crew-codex"]
    (root / ".odysseus-generated.json").write_text(json.dumps(manifest))
    (root / "agents" / "crew-api-glm-5-2").mkdir(parents=True)
    (root / "agents" / "crew-api-glm-5-2" / "config.yaml").write_text("name: crew-api-glm-5-2\n")

    with sqlite3.connect(root / "chat.db") as con:
        con.execute("CREATE TABLE agents (id BLOB, name TEXT, kind INTEGER)")
        legacy = bytes.fromhex("11" * 16)
        con.executemany("INSERT INTO agents VALUES (?, ?, ?)", [
            (bytes.fromhex("01" * 16), "crew", 1),
            (bytes.fromhex("02" * 16), "crew-glm-5-2", 1),
            (bytes.fromhex("03" * 16), "crew-codex", 1),
            (legacy, "crew-api-glm-5-2", 1),
            (bytes.fromhex("04" * 16), "glm-5-2", 1),
            (bytes.fromhex("05" * 16), "glm-5-2", 2),
            (bytes.fromhex("06" * 16), "polly", 1),
            (bytes.fromhex("07" * 16), "codex", 1),
        ])

    assert og._purge_generated_builtin_agent_rows() == 5
    with sqlite3.connect(root / "chat.db") as con:
        remaining = set(con.execute("SELECT name, kind FROM agents").fetchall())
    assert remaining == {("glm-5-2", 2), ("polly", 1), ("codex", 1)}


def test_refresh_moves_chats_from_retired_crews_onto_the_universal_crew(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    og._generate_crew({"zai-org/GLM-5.2": ("https://api.example/v1", "k")})
    root = tmp_path / "omnigent-home" / ".omnigent"
    manifest = json.loads((root / ".odysseus-generated.json").read_text())
    manifest["retired"] = ["crew-glm-5-2", "crew-codex"]
    (root / ".odysseus-generated.json").write_text(json.dumps(manifest))
    (root / "generated_agent_repoints.json").write_text(json.dumps({"legacy": "crew-glm-5-2"}))

    with sqlite3.connect(root / "chat.db") as con:
        con.execute("CREATE TABLE agents (id TEXT, name TEXT, session_id TEXT, bundle_location TEXT, version INTEGER)")
        con.execute("CREATE TABLE conversations (id TEXT, agent_id TEXT)")
        con.executemany("INSERT INTO agents VALUES (?, ?, ?, ?, ?)", [
            ("fresh", "crew", None, "new_bundle", 3),
            ("on_crew", "crew", "c1", "old_bundle", 1),
            ("on_glm_crew", "crew-glm-5-2", "c2", "b", 1),
            ("on_codex_crew", "crew-codex", "c3", "b", 1),
            ("raw_worker", "glm-5-2", "c4", "b", 1),
            ("mine", "my-agent", "c5", "b", 1),
        ])
        con.executemany("INSERT INTO conversations VALUES (?, ?)", [
            ("conv1", "on_crew"), ("conv2", "on_glm_crew"), ("conv3", "on_codex_crew"),
            ("conv4", "raw_worker"), ("conv5", "mine"), ("conv6", "legacy"),
        ])

    og._refresh_generated_session_agent_rows()

    with sqlite3.connect(root / "chat.db") as con:
        conv = dict(con.execute("SELECT id, agent_id FROM conversations").fetchall())
        bundle = con.execute("SELECT bundle_location FROM agents WHERE id='on_crew'").fetchone()[0]
    assert bundle == "new_bundle"
    assert conv == {"conv1": "fresh", "conv2": "fresh", "conv3": "fresh", "conv4": "fresh",
                    "conv5": "mine", "conv6": "fresh"}


def test_install_keeps_user_providers_and_includes_keyless_local_endpoints(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    from types import SimpleNamespace as NS

    cfg_dir = tmp_path / "omnigent-home" / ".omnigent"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({
        "providers": {
            "mine": {"kind": "gateway", "openai": {"base_url": "https://my.own/v1", "api_key": "x"},
                     "default": ["openai"]},
            "old-ours": {"kind": "gateway", "openai": {"base_url": "https://api.example/v1", "api_key": "old"}},
        },
        "harness": "claude-sdk",
    }))
    eps = [
        (NS(id="e1", name="Example", base_url="https://api.example/v1", api_key="new",
            pinned_models=None, cached_models=json.dumps(["deepseek-v4-flash"])), "api", "openai"),
        (NS(id="e2", name="Ollama", base_url="http://127.0.0.1:11434/v1", api_key=None,
            pinned_models=None, cached_models=json.dumps(["qwen3:8b", "nomic-embed-text"])), "local", "ollama"),
    ]
    monkeypatch.setattr(og, "_crew_endpoints", lambda user: eps)
    monkeypatch.setattr(og, "_purge_generated_builtin_agent_rows", lambda: 0)

    out = og._install_api_models("alice")

    cfg = yaml.safe_load((cfg_dir / "config.yaml").read_text())
    assert set(cfg["providers"]) == {"mine", "ody-example", "ody-ollama"}
    assert cfg["providers"]["mine"]["default"] == ["openai"]       # the user's default stays
    assert cfg["providers"]["ody-ollama"]["openai"]["api_key"] == "not-needed"
    assert cfg["harness"] == "claude-sdk"                           # not overridden
    assert out["models"] == 2                                       # embedding model skipped
    crew = _crew(tmp_path)
    assert "qwen3-8b" in crew["tools"]["agents"] and "deepseek-v4-flash" in crew["tools"]["agents"]
    assert "free (local hardware)" in crew["prompt"]


def test_launch_requires_admin(monkeypatch):
    import routes.omnigent_routes as og
    from fastapi import HTTPException

    def deny(request):
        raise HTTPException(403, "Admin only")

    monkeypatch.setattr(og, "require_admin", deny)
    launch = _handler(og.setup_omnigent_routes(_FakeManager()), "POST", "/api/omnigent/launch")
    with pytest.raises(HTTPException) as exc:
        launch(SimpleNamespace(state=SimpleNamespace(current_user="bob")))
    assert exc.value.status_code == 403


def test_orchestrator_route_validates_choice_and_saves(tmp_path, monkeypatch):
    og = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(og, "require_admin", lambda request: None)
    monkeypatch.setattr(og, "orchestrator_options", lambda user: [{"id": "claude"}, {"id": "codex::gpt-5.5"}])
    monkeypatch.setattr(og, "_install_api_models", lambda user: {"workers": 2})
    router = og.setup_omnigent_routes(_FakeManager())
    set_orch = _handler(router, "POST", "/api/omnigent/orchestrator")

    class Req:
        state = SimpleNamespace(current_user="admin")

        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(set_orch(Req({"orchestrator": "api::x::not-offered"})))
    assert exc.value.status_code == 400

    out = asyncio.run(set_orch(Req({"orchestrator": "codex::gpt-5.5", "reasoning_effort": "xhigh"})))
    assert out["restarted"] is False                       # server not running: files only
    assert og.load_crew_settings()["orchestrator"] == "codex::gpt-5.5"
    assert og.load_crew_settings()["reasoning_effort"] == "xhigh"


def test_apply_during_a_launch_saves_nothing_and_pending_restart_is_reported(tmp_path, monkeypatch):
    """Found live: applying a new orchestrator while the panel's auto-launch was
    starting saved the choice but left the server on the old crew, silently."""
    og = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(og, "require_admin", lambda request: None)
    monkeypatch.setattr(og, "orchestrator_options", lambda user: [{"id": "claude"}, {"id": "codex::gpt-5.5"}])
    router = og.setup_omnigent_routes(_FakeManager())
    set_orch = _handler(router, "POST", "/api/omnigent/orchestrator")

    class Req:
        state = SimpleNamespace(current_user="admin")

        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    from fastapi import HTTPException
    assert og._LAUNCH_LOCK.acquire(blocking=False)          # a launch is in flight
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(set_orch(Req({"orchestrator": "codex::gpt-5.5"})))
        assert exc.value.status_code == 409
        assert og.load_crew_settings()["orchestrator"] == "claude"      # nothing saved
    finally:
        og._LAUNCH_LOCK.release()

    og.mark_crew_launched(og.load_crew_settings())
    assert og.crew_pending_restart(True) is False
    og.save_crew_settings({**og.load_crew_settings(), "orchestrator": "codex::gpt-5.5"})
    assert og.crew_pending_restart(True) is True
    assert og.crew_pending_restart(False) is False                       # nothing running, nothing stale


def test_omnigent_env_drops_secrets_but_keeps_harness_logins():
    from src.omnigent_manager import scrubbed_env

    env = scrubbed_env({
        "PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8",
        "SMTP_PASSWORD": "x", "GOOGLE_OAUTH_CLIENT_SECRET": "x", "DATABASE_URL": "postgres://u:p@h/db",
        "OPENAI_API_KEY": "kept-for-gateway", "ANTHROPIC_API_KEY": "x", "ODYSSEUS_ADMIN_PASSWORD": "x",
        "CLAUDE_CODE_OAUTH_TOKEN": "kept", "OMNIGENT_UI_PORT": "6868", "SOME_TOKEN": "x",
    })
    assert set(env) == {"PATH", "HOME", "LANG", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OMNIGENT_UI_PORT"}
