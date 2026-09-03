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
    for gone in ("/api/omnigent/agents", "/api/omnigent/orchestrator",
                 "/api/omnigent/agents/compile", "/api/omnigent/model-options"):
        assert gone not in paths, f"{gone} should be removed"
    # the launcher endpoints stay
    assert "/api/omnigent/launch" in paths
    assert "/api/omnigent/status" in paths


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
    # no models -> no crew written, no crash
    assert _generate_crew({}, None) == 0


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


def test_pick_best_api_models_keeps_requested_crews():
    from routes.omnigent_routes import _pick_best_api_models

    picked = _pick_best_api_models([
        "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "deepseek-ai/DeepSeek-V4-Flash",
        "zai-org/GLM-5.2",
        "deepseek-ai/DeepSeek-V4-Pro",
        "moonshotai/Kimi-K2.6",
        "Qwen/Qwen3-Coder-480B-A35B-Instruct",
    ])

    assert picked[:4] == [
        "zai-org/GLM-5.2",
        "deepseek-ai/DeepSeek-V4-Flash",
        "deepseek-ai/DeepSeek-V4-Pro",
        "moonshotai/Kimi-K2.6",
    ]
    assert "Qwen/Qwen3-Coder-480B-A35B-Instruct" in picked


def test_generate_crew_writes_broad_and_curated_crew_variants(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _generate_crew

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    agents_root = tmp_path / "omnigent-home" / ".omnigent" / "agents"
    stale_worker = agents_root / "crew" / "agents" / "stale-openai-worker"
    stale_worker.mkdir(parents=True)
    (stale_worker / "config.yaml").write_text("name: stale-openai-worker\n")

    models = {
        "zai-org/GLM-5.2": ("https://api.inference.wandb.ai/v1", "wandb_key"),
        "deepseek-ai/DeepSeek-V4-Flash": ("https://api.inference.wandb.ai/v1", "wandb_key"),
        "deepseek-ai/DeepSeek-V4-Pro": ("https://api.inference.wandb.ai/v1", "wandb_key"),
        "Qwen/Qwen3-Coder-480B-A35B-Instruct": ("https://api.inference.wandb.ai/v1", "wandb_key"),
    }

    written = _generate_crew(models, "zai-org/GLM-5.2")

    assert written == 7
    assert not stale_worker.exists()
    assert not list(agents_root.glob("crew-api-*"))

    crew = yaml.safe_load((agents_root / "crew" / "config.yaml").read_text())
    assert crew["executor"]["config"]["harness"] == "claude-sdk"
    assert {"claude-code", "codex", "glm-5-2", "deepseek-v4-flash", "deepseek-v4-pro"} <= set(
        crew["tools"]["agents"]
    )

    crew_claude = yaml.safe_load((agents_root / "crew-claude" / "config.yaml").read_text())
    assert crew_claude["executor"]["config"]["harness"] == "claude-sdk"

    crew_codex = yaml.safe_load((agents_root / "crew-codex" / "config.yaml").read_text())
    assert crew_codex["executor"]["config"]["harness"] == "codex-native"
    assert crew_codex["executor"]["config"]["yolo"] is True
    assert crew_codex["executor"]["model"] == "gpt-5.6-sol"
    assert crew_codex["executor"]["config"]["reasoning_effort"] == "xhigh"
    assert crew_codex["llm"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }

    glm_crew = yaml.safe_load((agents_root / "crew-glm-5-2" / "config.yaml").read_text())
    assert glm_crew["executor"]["config"]["harness"] == "openai-agents"
    assert glm_crew["executor"]["model"] == "zai-org/GLM-5.2"
    assert glm_crew["executor"]["auth"]["api_key"] == "wandb_key"
    assert glm_crew["executor"]["auth"]["base_url"] == "https://api.inference.wandb.ai/v1"
    # Per-model crews are API-model-brained without Codex/Claude to save subscription usage
    assert glm_crew["tools"]["agents"] == []
    assert "running directly on `zai-org/GLM-5.2`" in glm_crew["prompt"]
    assert "anchored on `glm-5-2`" not in glm_crew["prompt"]
    assert not (agents_root / "crew-glm-5-2" / "agents" / "glm-5-2").exists()
    # No Codex/Claude sub-agent should be written inside per-model crew
    assert not (agents_root / "crew-glm-5-2" / "agents" / "codex").exists()
    assert not (agents_root / "crew-glm-5-2" / "agents" / "claude-code").exists()

    glm_worker = yaml.safe_load((agents_root / "crew" / "agents" / "glm-5-2" / "config.yaml").read_text())
    assert glm_worker["executor"]["config"]["harness"] == "openai-agents"
    assert glm_worker["executor"]["model"] == "zai-org/GLM-5.2"
    assert glm_worker["executor"]["auth"]["api_key"] == "wandb_key"
    assert glm_worker["executor"]["auth"]["base_url"] == "https://api.inference.wandb.ai/v1"


def test_generate_crew_creates_qwen_27b_variant_without_codex_subagent(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _generate_crew

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    agents_root = tmp_path / "omnigent-home" / ".omnigent" / "agents"

    models = {
        "zai-org/GLM-5.2": ("https://api.inference.wandb.ai/v1", "k1"),
        "Qwen/Qwen3-27B": ("https://api.inference.wandb.ai/v1", "k2"),
        "Qwen/Qwen2.5-27B-Instruct": ("https://api.inference.wandb.ai/v1", "k3"),
    }

    written = _generate_crew(models, "Qwen/Qwen3-27B")

    # Qwen 27B should get a dedicated crew via _BEST_API_MODEL_HINTS ("27b")
    assert (agents_root / "crew-qwen3-27b" / "config.yaml").exists() or (agents_root / "crew-qwen2-5-27b-instruct" / "config.yaml").exists()
    # Find the qwen crew that was created
    qwen_crew_path = None
    for p in agents_root.glob("crew-qwen*27b*"):
        if (p / "config.yaml").exists():
            qwen_crew_path = p
            break
    assert qwen_crew_path is not None, "Qwen 27B crew not created"
    cfg = yaml.safe_load((qwen_crew_path / "config.yaml").read_text())
    # Must run directly on Qwen model and have no Codex/Claude sub-agents
    assert cfg["executor"]["model"] in ("Qwen/Qwen3-27B", "Qwen/Qwen2.5-27B-Instruct")
    assert cfg["tools"]["agents"] == []
    assert not (qwen_crew_path / "agents" / "codex").exists()
    assert not (qwen_crew_path / "agents" / "claude-code").exists()
    # Broad crews still retain Codex/Claude for deep work
    crew = yaml.safe_load((agents_root / "crew" / "config.yaml").read_text())
    assert "codex" in crew["tools"]["agents"]
    assert "claude-code" in crew["tools"]["agents"]


def test_builtin_agent_env_registers_only_top_level_crews(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _builtin_agent_env, _generate_crew

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    _generate_crew(
        {"zai-org/GLM-5.2": ("https://api.inference.wandb.ai/v1", "wandb_key")},
        "zai-org/GLM-5.2",
    )

    env = _builtin_agent_env()
    names = {Path(p).name for p in env["OMNIGENT_BUILTIN_AGENT_DIRS"].split(os.pathsep)}

    assert {"crew", "crew-claude", "crew-codex", "crew-glm-5-2"} <= names
    assert "glm-5-2" not in names
    assert not any(name.startswith("crew-api-") for name in names)


def test_purge_generated_builtin_agent_rows_removes_stale_crews_and_raw_workers(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _purge_generated_builtin_agent_rows

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    root = tmp_path / "omnigent-home" / ".omnigent"
    agents_root = root / "agents"
    for agent_name in ("crew", "crew-glm-5-2", "crew-api-glm-5-2"):
        agent_dir = agents_root / agent_name
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text(f"name: {agent_name}\n")
    raw_worker = agents_root / "crew" / "agents" / "glm-5-2"
    raw_worker.mkdir(parents=True)
    (raw_worker / "config.yaml").write_text("name: glm-5-2\n")
    cli_worker = agents_root / "crew" / "agents" / "codex"
    cli_worker.mkdir(parents=True)
    (cli_worker / "config.yaml").write_text("name: codex\n")

    db_path = root / "chat.db"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE agents (id TEXT, name TEXT, session_id TEXT)")
        con.executemany(
            "INSERT INTO agents VALUES (?, ?, ?)",
            [
                ("fresh_crew", "crew", None),
                ("legacy_api", "crew-api-glm-5-2", None),
                ("fresh_glm_crew", "crew-glm-5-2", None),
                ("raw_glm", "glm-5-2", None),
                ("session_glm", "glm-5-2", "conv_keep"),
                ("polly", "polly", None),
                ("codex", "codex", None),
            ],
        )
        con.commit()

    purged = _purge_generated_builtin_agent_rows()

    assert purged == 4
    with sqlite3.connect(db_path) as con:
        remaining = set(con.execute("SELECT name, session_id FROM agents").fetchall())
    assert remaining == {("glm-5-2", "conv_keep"), ("polly", None), ("codex", None)}
    repoints = json.loads((root / "generated_agent_repoints.json").read_text())
    assert repoints == {"legacy_api": "crew-glm-5-2"}


def test_refresh_generated_session_agent_rows_repoints_existing_chats(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _refresh_generated_session_agent_rows

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    root = tmp_path / "omnigent-home" / ".omnigent"
    crew_dir = root / "agents" / "crew"
    crew_dir.mkdir(parents=True)
    (crew_dir / "config.yaml").write_text("name: crew\n")
    deepseek_worker = crew_dir / "agents" / "deepseek-v3-1"
    deepseek_worker.mkdir(parents=True)
    (deepseek_worker / "config.yaml").write_text("name: deepseek-v3-1\n")
    codex_dir = root / "agents" / "crew-codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.yaml").write_text("name: crew-codex\n")
    agent_dir = root / "agents" / "crew-glm-5-2"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text("name: crew-glm-5-2\n")
    (root / "generated_agent_repoints.json").write_text(json.dumps({"legacy_api": "crew-glm-5-2"}))

    old_openai_bundle = root / "artifacts" / "ag_openai" / "bundle"
    old_openai_bundle.parent.mkdir(parents=True)
    old_openai_config = (
        "name: openai-agents\n"
        "executor:\n"
        "  model: zai-org/GLM-5.2\n"
        "  config:\n"
        "    harness: openai-agents\n"
    ).encode()
    with tarfile.open(old_openai_bundle, "w:gz") as tf:
        info = tarfile.TarInfo("./config.yaml")
        info.size = len(old_openai_config)
        tf.addfile(info, BytesIO(old_openai_config))

    db_path = root / "chat.db"
    with sqlite3.connect(db_path) as con:
        con.execute(
            "CREATE TABLE agents (id TEXT, name TEXT, session_id TEXT, bundle_location TEXT, version INTEGER)"
        )
        con.execute("CREATE TABLE conversations (id TEXT, agent_id TEXT)")
        con.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?)",
            [
                ("fresh_codex", "crew-codex", None, "new_codex_bundle", 2),
                ("fresh", "crew-glm-5-2", None, "new_bundle", 7),
                ("session", "crew-glm-5-2", "conv_session", "old_bundle", 1),
                ("raw_glm", "glm-5-2", "conv_raw_glm", "raw_bundle", 1),
                ("legacy_session_api", "crew-api-glm-5-2", "conv_session_api", "old_api_bundle", 1),
                ("old_openai", "openai-agents", "conv_openai", "ag_openai/bundle", 1),
                ("raw_v3", "deepseek-v3-1", "conv_raw_v3", "old_v3_bundle", 1),
            ],
        )
        con.executemany(
            "INSERT INTO conversations VALUES (?, ?)",
            [
                ("conv_session", "session"),
                ("conv_raw_glm", "raw_glm"),
                ("conv_session_api", "legacy_session_api"),
                ("conv_openai", "old_openai"),
                ("conv_raw_v3", "raw_v3"),
                ("conv_legacy", "legacy_api"),
                ("conv_other", "other"),
            ],
        )
        con.commit()

    refreshed = _refresh_generated_session_agent_rows()

    assert refreshed == 11
    with sqlite3.connect(db_path) as con:
        session_row = con.execute(
            "SELECT bundle_location, version FROM agents WHERE id = 'session'"
        ).fetchone()
        conversations = dict(con.execute("SELECT id, agent_id FROM conversations").fetchall())
        stale_agent_ids = {
            row[0]
            for row in con.execute(
                "SELECT id FROM agents WHERE id IN "
                "('raw_glm', 'legacy_session_api', 'old_openai', 'raw_v3')"
            ).fetchall()
        }
    assert session_row == ("new_bundle", 7)
    assert conversations["conv_session"] == "fresh"
    assert conversations["conv_raw_glm"] == "fresh"
    assert conversations["conv_session_api"] == "fresh"
    assert conversations["conv_openai"] == "fresh"
    assert conversations["conv_raw_v3"] == "fresh_codex"
    assert conversations["conv_legacy"] == "fresh"
    assert conversations["conv_other"] == "other"
    assert stale_agent_ids == set()
    assert not (root / "generated_agent_repoints.json").exists()


def test_purge_generated_builtin_agent_rows_supports_kind_schema_and_blob_ids(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _purge_generated_builtin_agent_rows

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    root = tmp_path / "omnigent-home" / ".omnigent"
    agents_root = root / "agents"
    for agent_name in ("crew", "crew-glm-5-2", "crew-api-glm-5-2"):
        agent_dir = agents_root / agent_name
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text(f"name: {agent_name}\n")
    raw_worker = agents_root / "crew" / "agents" / "glm-5-2"
    raw_worker.mkdir(parents=True)
    (raw_worker / "config.yaml").write_text("name: glm-5-2\n")

    legacy_id = bytes.fromhex("11" * 16)
    db_path = root / "chat.db"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE agents (id BLOB, name TEXT, kind INTEGER)")
        con.executemany(
            "INSERT INTO agents VALUES (?, ?, ?)",
            [
                (bytes.fromhex("01" * 16), "crew", 1),
                (legacy_id, "crew-api-glm-5-2", 1),
                (bytes.fromhex("02" * 16), "crew-glm-5-2", 1),
                (bytes.fromhex("03" * 16), "glm-5-2", 1),
                (bytes.fromhex("04" * 16), "glm-5-2", 2),
                (bytes.fromhex("05" * 16), "polly", 1),
                (bytes.fromhex("06" * 16), "codex", 1),
            ],
        )
        con.commit()

    purged = _purge_generated_builtin_agent_rows()

    assert purged == 4
    with sqlite3.connect(db_path) as con:
        remaining = set(con.execute("SELECT name, kind FROM agents").fetchall())
    assert remaining == {("glm-5-2", 2), ("polly", 1), ("codex", 1)}
    repoints = json.loads((root / "generated_agent_repoints.json").read_text())
    assert repoints == {f"hex:{legacy_id.hex()}": "crew-glm-5-2"}


def test_refresh_generated_session_agent_rows_supports_kind_schema_and_blob_ids(tmp_path, monkeypatch):
    import routes.omnigent_routes as omnigent_routes
    from routes.omnigent_routes import _refresh_generated_session_agent_rows

    monkeypatch.setattr(omnigent_routes, "DATA_DIR", str(tmp_path))
    root = tmp_path / "omnigent-home" / ".omnigent"
    crew_dir = root / "agents" / "crew"
    crew_dir.mkdir(parents=True)
    (crew_dir / "config.yaml").write_text("name: crew\n")
    deepseek_worker = crew_dir / "agents" / "deepseek-v3-1"
    deepseek_worker.mkdir(parents=True)
    (deepseek_worker / "config.yaml").write_text("name: deepseek-v3-1\n")
    codex_dir = root / "agents" / "crew-codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.yaml").write_text("name: crew-codex\n")
    agent_dir = root / "agents" / "crew-glm-5-2"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text("name: crew-glm-5-2\n")

    ids = {name: bytes([idx]) * 16 for idx, name in enumerate(
        ("fresh_codex", "fresh", "session", "raw_glm", "legacy_session_api", "old_openai", "raw_v3", "legacy"),
        start=1,
    )}
    (root / "generated_agent_repoints.json").write_text(json.dumps({
        f"hex:{ids['legacy'].hex()}": "crew-glm-5-2",
    }))

    old_openai_bundle = root / "artifacts" / "ag_openai" / "bundle"
    old_openai_bundle.parent.mkdir(parents=True)
    old_openai_config = (
        "name: openai-agents\n"
        "executor:\n"
        "  model: zai-org/GLM-5.2\n"
        "  config:\n"
        "    harness: openai-agents\n"
    ).encode()
    with tarfile.open(old_openai_bundle, "w:gz") as tf:
        info = tarfile.TarInfo("./config.yaml")
        info.size = len(old_openai_config)
        tf.addfile(info, BytesIO(old_openai_config))

    db_path = root / "chat.db"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE agents (id BLOB, name TEXT, bundle_location TEXT, version INTEGER, kind INTEGER)")
        con.execute("CREATE TABLE conversations (id TEXT, agent_id BLOB)")
        con.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?)",
            [
                (ids["fresh_codex"], "crew-codex", "new_codex_bundle", 2, 1),
                (ids["fresh"], "crew-glm-5-2", "new_bundle", 7, 1),
                (ids["session"], "crew-glm-5-2", "old_bundle", 1, 2),
                (ids["raw_glm"], "glm-5-2", "raw_bundle", 1, 2),
                (ids["legacy_session_api"], "crew-api-glm-5-2", "old_api_bundle", 1, 2),
                (ids["old_openai"], "openai-agents", "ag_openai/bundle", 1, 2),
                (ids["raw_v3"], "deepseek-v3-1", "old_v3_bundle", 1, 2),
            ],
        )
        con.executemany(
            "INSERT INTO conversations VALUES (?, ?)",
            [
                ("conv_session", ids["session"]),
                ("conv_raw_glm", ids["raw_glm"]),
                ("conv_session_api", ids["legacy_session_api"]),
                ("conv_openai", ids["old_openai"]),
                ("conv_raw_v3", ids["raw_v3"]),
                ("conv_legacy", ids["legacy"]),
                ("conv_other", bytes.fromhex("ff" * 16)),
            ],
        )
        con.commit()

    refreshed = _refresh_generated_session_agent_rows()

    assert refreshed == 11
    with sqlite3.connect(db_path) as con:
        session_row = con.execute(
            "SELECT bundle_location, version FROM agents WHERE id = ?", (ids["session"],)
        ).fetchone()
        conversations = dict(con.execute("SELECT id, agent_id FROM conversations").fetchall())
        stale_agent_ids = {
            row[0]
            for row in con.execute(
                "SELECT id FROM agents WHERE id IN (?, ?, ?, ?)",
                (ids["raw_glm"], ids["legacy_session_api"], ids["old_openai"], ids["raw_v3"]),
            ).fetchall()
        }
    assert session_row == ("new_bundle", 7)
    assert conversations["conv_session"] == ids["fresh"]
    assert conversations["conv_raw_glm"] == ids["fresh"]
    assert conversations["conv_session_api"] == ids["fresh"]
    assert conversations["conv_openai"] == ids["fresh"]
    assert conversations["conv_raw_v3"] == ids["fresh_codex"]
    assert conversations["conv_legacy"] == ids["fresh"]
    assert conversations["conv_other"] == bytes.fromhex("ff" * 16)
    assert stale_agent_ids == set()
    assert not (root / "generated_agent_repoints.json").exists()
