# Omnigent Custom Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user define/save their own Omnigent agents (per-agent backend + model) and a customizable orchestrator, persisted per-user, compiling into a runnable Omnigent `config.yaml` with a Claude-Code-grade toolset.

**Architecture:** A new owner-scoped JSON store (`OmnigentAgentStore`) + a pure `compile_config()` that emits an Omnigent spec (reusing the canonical Odysseus tools from `integrations/omnigent/config.yaml`). New owner-scoped endpoints under `/api/omnigent`. The existing `static/js/omnigent.js` modal gains an Orchestrator control + an Agents CRUD section.

**Tech Stack:** Python/FastAPI, SQLAlchemy (`ModelEndpoint` for API models), PyYAML (new), vanilla-JS ES module frontend, pytest.

> **Commit policy:** This repo's owner commits/pushes only when explicitly asked. Commit steps below are local and structural — **confirm with the owner before the first commit**, then batch as they prefer. Never push.

**Spec:** `docs/superpowers/specs/2026-06-25-omnigent-agents-design.md`

---

## File Structure

- **Create** `src/omnigent_agents.py` — `OmnigentAgentStore` (CRUD + orchestrator + atomic persistence) and pure `compile_config(agents, orchestrator)` + helpers. One responsibility: agent/orchestrator config state & compilation.
- **Create** `tests/test_omnigent_agents.py` — store CRUD + owner isolation + compile validity.
- **Modify** `routes/omnigent_routes.py` — inject the store; add `/agents` CRUD, `/orchestrator`, `/model-options`, `/agents/compile`.
- **Modify** `tests/test_omnigent_routes.py` — API tests for the new endpoints (existing `_handler`/monkeypatch pattern).
- **Modify** `requirements.txt` — add `PyYAML`.
- **Modify** `static/js/omnigent.js` — Orchestrator control + Agents section + add/edit/delete modal + "View run config".
- **Modify** `tests/test_omnigent_static.py` — assert new JS markers exist.

---

## Task 1: Agent store — CRUD + persistence

**Files:**
- Create: `src/omnigent_agents.py`
- Test: `tests/test_omnigent_agents.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add PyYAML to requirements**

In `requirements.txt`, add a line (near the other top-level deps):
```
PyYAML
```
Install locally: `pip install PyYAML`

- [ ] **Step 2: Write the failing test**

Create `tests/test_omnigent_agents.py`:
```python
"""Omnigent agent store + config compilation tests."""

from __future__ import annotations


def test_create_list_get_are_owner_scoped(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    agent = store.create_agent(
        owner="alice", name="Researcher",
        role="Find context.", backend="api",
        model={"endpoint_id": "ep1", "model": "kimi-k2.6"},
    )
    assert agent["owner"] == "alice"
    assert agent["backend"] == "api"
    assert agent["enabled"] is True
    assert agent["id"].startswith("agent-")
    assert store.list_agents("bob") == []
    assert store.list_agents("alice")[0]["id"] == agent["id"]
    assert store.get_agent(agent["id"], owner="bob") is None
    assert store.get_agent(agent["id"], owner="alice")["name"] == "Researcher"


def test_update_and_delete_owner_scoped(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    agent = store.create_agent(owner="alice", name="Coder", role="Build.", backend="claude-subscription")

    updated = store.update_agent(agent["id"], owner="alice", name="Coder 2", enabled=False)
    assert updated["name"] == "Coder 2"
    assert updated["enabled"] is False
    assert store.update_agent(agent["id"], owner="bob", name="x") is None

    assert store.delete_agent(agent["id"], owner="bob") is False
    assert store.delete_agent(agent["id"], owner="alice") is True
    assert store.list_agents("alice") == []


def test_create_validates_name_and_backend(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    for bad in (lambda: store.create_agent(owner="a", name=" ", role="", backend="api"),
                lambda: store.create_agent(owner="a", name="X", role="", backend="bogus")):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_omnigent_agents.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.omnigent_agents'`

- [ ] **Step 4: Write minimal implementation**

Create `src/omnigent_agents.py`:
```python
"""Owner-scoped store + compiler for user-defined Omnigent agents."""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.constants import DATA_DIR

STATE_FILE = Path(DATA_DIR) / "omnigent_agents.json"

BACKENDS = {"claude-subscription", "chatgpt-subscription", "api"}
HARNESS_BY_BACKEND = {
    "claude-subscription": "claude-sdk",
    "chatgpt-subscription": "codex",
    "api": "claude-sdk",
}
DEFAULT_ORCHESTRATOR = {"backend": "claude-subscription", "model": None, "workspace": ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any) -> Any:
    return deepcopy(value)


class OmnigentAgentStore:
    """Persist user-defined agents + a per-owner orchestrator config."""

    def __init__(self, state_path: str | os.PathLike[str] | None = None):
        self.state_path = Path(state_path) if state_path is not None else STATE_FILE

    # ---- agents ---------------------------------------------------------
    def list_agents(self, owner: str | None) -> list[dict[str, Any]]:
        agents = [a for a in self._load()["agents"] if a.get("owner") == owner]
        agents.sort(key=lambda a: a.get("created_at") or "")
        return _copy(agents)

    def get_agent(self, agent_id: str, owner: str | None) -> dict[str, Any] | None:
        for a in self._load()["agents"]:
            if a.get("id") == agent_id and a.get("owner") == owner:
                return _copy(a)
        return None

    def create_agent(self, owner: str | None, name: str, role: str, backend: str,
                     model: dict | None = None, enabled: bool = True) -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Agent name is required")
        if backend not in BACKENDS:
            raise ValueError(f"Unknown backend: {backend}")
        now = _now()
        agent = {
            "id": f"agent-{uuid.uuid4().hex[:12]}",
            "owner": owner,
            "name": clean_name,
            "role": (role or "").strip(),
            "backend": backend,
            "model": model if backend == "api" else None,
            "enabled": bool(enabled),
            "created_at": now,
            "updated_at": now,
        }
        state = self._load()
        state["agents"].append(agent)
        self._save(state)
        return _copy(agent)

    def update_agent(self, agent_id: str, owner: str | None, **fields: Any) -> dict[str, Any] | None:
        state = self._load()
        for a in state["agents"]:
            if a.get("id") == agent_id and a.get("owner") == owner:
                if "name" in fields:
                    name = (fields["name"] or "").strip()
                    if not name:
                        raise ValueError("Agent name is required")
                    a["name"] = name
                if "role" in fields:
                    a["role"] = (fields["role"] or "").strip()
                if "backend" in fields:
                    if fields["backend"] not in BACKENDS:
                        raise ValueError(f"Unknown backend: {fields['backend']}")
                    a["backend"] = fields["backend"]
                if "model" in fields:
                    a["model"] = fields["model"] if a["backend"] == "api" else None
                if "enabled" in fields:
                    a["enabled"] = bool(fields["enabled"])
                a["updated_at"] = _now()
                self._save(state)
                return _copy(a)
        return None

    def delete_agent(self, agent_id: str, owner: str | None) -> bool:
        state = self._load()
        before = len(state["agents"])
        state["agents"] = [a for a in state["agents"]
                           if not (a.get("id") == agent_id and a.get("owner") == owner)]
        if len(state["agents"]) == before:
            return False
        self._save(state)
        return True

    # ---- orchestrator (added in Task 2) ---------------------------------

    # ---- persistence ----------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"agents": [], "orchestrators": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"agents": [], "orchestrators": {}}
        if not isinstance(data, dict):
            return {"agents": [], "orchestrators": {}}
        data.setdefault("agents", [])
        data.setdefault("orchestrators", {})
        return data

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_omnigent_agents.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit** (confirm with owner first — see Commit policy)

```bash
git add requirements.txt src/omnigent_agents.py tests/test_omnigent_agents.py
git commit -m "feat(omnigent): owner-scoped agent config store"
```

---

## Task 2: Orchestrator get/set

**Files:**
- Modify: `src/omnigent_agents.py` (replace the `# ---- orchestrator` placeholder)
- Test: `tests/test_omnigent_agents.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_omnigent_agents.py`:
```python
def test_orchestrator_defaults_and_set_owner_scoped(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    # default when never set
    assert store.get_orchestrator("alice") == {
        "backend": "claude-subscription", "model": None, "workspace": ""
    }
    saved = store.set_orchestrator("alice", backend="api",
                                   model={"endpoint_id": "ep1", "model": "gpt-x"},
                                   workspace="/proj")
    assert saved["backend"] == "api"
    assert saved["model"]["model"] == "gpt-x"
    assert saved["workspace"] == "/proj"
    assert store.get_orchestrator("alice")["backend"] == "api"
    assert store.get_orchestrator("bob")["backend"] == "claude-subscription"


def test_set_orchestrator_rejects_bad_backend(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    try:
        store.set_orchestrator("alice", backend="nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_omnigent_agents.py -k orchestrator -v`
Expected: FAIL with `AttributeError: 'OmnigentAgentStore' object has no attribute 'get_orchestrator'`

- [ ] **Step 3: Write minimal implementation**

In `src/omnigent_agents.py`, replace the `# ---- orchestrator (added in Task 2) ---` line with:
```python
    def get_orchestrator(self, owner: str | None) -> dict[str, Any]:
        stored = self._load()["orchestrators"].get(str(owner))
        if not isinstance(stored, dict):
            return _copy(DEFAULT_ORCHESTRATOR)
        return {
            "backend": stored.get("backend", "claude-subscription"),
            "model": stored.get("model"),
            "workspace": stored.get("workspace", ""),
        }

    def set_orchestrator(self, owner: str | None, backend: str,
                         model: dict | None = None, workspace: str = "") -> dict[str, Any]:
        if backend not in BACKENDS:
            raise ValueError(f"Unknown backend: {backend}")
        record = {
            "backend": backend,
            "model": model if backend == "api" else None,
            "workspace": (workspace or "").strip(),
            "updated_at": _now(),
        }
        state = self._load()
        state["orchestrators"][str(owner)] = record
        self._save(state)
        return self.get_orchestrator(owner)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_omnigent_agents.py -k orchestrator -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit** (per Commit policy)

```bash
git add src/omnigent_agents.py tests/test_omnigent_agents.py
git commit -m "feat(omnigent): per-owner orchestrator config"
```

---

## Task 3: Compile to Omnigent config.yaml

**Files:**
- Modify: `src/omnigent_agents.py` (add module-level compile functions)
- Test: `tests/test_omnigent_agents.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_omnigent_agents.py`:
```python
def test_compile_config_emits_valid_omnigent_spec():
    import yaml
    from src.omnigent_agents import compile_config

    agents = [
        {"name": "Researcher", "role": "Find context.", "backend": "api",
         "model": {"endpoint_id": "ep1", "model": "kimi-k2.6"}, "enabled": True},
        {"name": "Coder", "role": "Build it.", "backend": "claude-subscription",
         "model": None, "enabled": True},
        {"name": "Disabled", "role": "x", "backend": "api", "model": None, "enabled": False},
    ]
    orchestrator = {"backend": "claude-subscription", "model": None, "workspace": "/work"}

    text = compile_config(agents, orchestrator)
    spec = yaml.safe_load(text)

    assert spec["spec_version"] == 1
    assert spec["executor"]["config"]["harness"] == "claude-sdk"
    assert spec["workspace"] == "/work"
    names = [a["name"] for a in spec["agents"]]
    assert names == ["Researcher", "Coder"]               # disabled excluded
    assert spec["agents"][0]["harness"] == "claude-sdk"
    assert spec["agents"][0]["model"] == "kimi-k2.6"
    # canonical Odysseus tools are reused
    assert "odysseus_capabilities" in spec["tools"]


def test_compile_chatgpt_orchestrator_uses_codex_harness():
    import yaml
    from src.omnigent_agents import compile_config

    text = compile_config([], {"backend": "chatgpt-subscription", "model": None, "workspace": ""})
    spec = yaml.safe_load(text)
    assert spec["executor"]["config"]["harness"] == "codex"
    assert spec["agents"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_omnigent_agents.py -k compile -v`
Expected: FAIL with `ImportError: cannot import name 'compile_config'`

- [ ] **Step 3: Write minimal implementation**

In `src/omnigent_agents.py`, add at module level (after `DEFAULT_ORCHESTRATOR`):
```python
_CONFIG_YAML = Path(__file__).resolve().parent.parent / "integrations" / "omnigent" / "config.yaml"


def _odysseus_tools() -> dict[str, Any]:
    """Reuse the canonical scoped Odysseus tools (single source of truth)."""
    try:
        data = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tools"), dict):
            return data["tools"]
    except Exception:
        pass
    return {}


def _orchestrator_prompt(orch: dict[str, Any]) -> str:
    return (
        "You are the Odysseus orchestrator running inside Omnigent.\n"
        "Coordinate the configured agents to accomplish the user's goal.\n"
        "You and the agents have Claude Code-grade tools (file read/write/edit, shell,\n"
        "search, web) plus the scoped Odysseus tools. Treat email sending, destructive\n"
        "changes, and cookbook launches as high-impact actions needing confirmation."
    )


def _agent_block(agent: dict[str, Any]) -> dict[str, Any]:
    backend = agent.get("backend", "claude-subscription")
    block: dict[str, Any] = {
        "name": agent.get("name", "agent"),
        "prompt": agent.get("role") or "",
        "harness": HARNESS_BY_BACKEND.get(backend, "claude-sdk"),
    }
    model = agent.get("model")
    if backend == "api" and isinstance(model, dict):
        if model.get("model"):
            block["model"] = model["model"]
        if model.get("endpoint_id"):
            block["model_endpoint"] = model["endpoint_id"]
    return block


def compile_config(agents: list[dict[str, Any]], orchestrator: dict[str, Any]) -> str:
    """Render saved agents + orchestrator into an Omnigent config.yaml (text)."""
    orch = orchestrator or {}
    backend = orch.get("backend", "claude-subscription")
    executor_config: dict[str, Any] = {"harness": HARNESS_BY_BACKEND.get(backend, "claude-sdk")}
    if backend == "api" and isinstance(orch.get("model"), dict):
        if orch["model"].get("model"):
            executor_config["model"] = orch["model"]["model"]
        if orch["model"].get("endpoint_id"):
            executor_config["model_endpoint"] = orch["model"]["endpoint_id"]
    config = {
        "spec_version": 1,
        "name": "odysseus",
        "description": "Odysseus-managed Omnigent crew.",
        "executor": {"type": "omnigent", "config": executor_config},
        "prompt": _orchestrator_prompt(orch),
        "workspace": orch.get("workspace") or ".omnigent-workspace",
        "agents": [_agent_block(a) for a in agents if a.get("enabled", True)],
        "tools": _odysseus_tools(),
    }
    return yaml.safe_dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_omnigent_agents.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit** (per Commit policy)

```bash
git add src/omnigent_agents.py tests/test_omnigent_agents.py
git commit -m "feat(omnigent): compile saved agents into config.yaml"
```

---

## Task 4: API — agents CRUD endpoints

**Files:**
- Modify: `routes/omnigent_routes.py`
- Test: `tests/test_omnigent_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_omnigent_routes.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_omnigent_routes.py -k agents_crud -v`
Expected: FAIL with `TypeError: setup_omnigent_routes() got an unexpected keyword argument 'agent_store'`

- [ ] **Step 3: Write minimal implementation**

In `routes/omnigent_routes.py`:

(a) Add import near the others:
```python
from src.omnigent_agents import OmnigentAgentStore, compile_config
```

(b) Change the signature + construction:
```python
def setup_omnigent_routes(
    manager: OmnigentManager | None = None,
    native_manager: NativeOmnigentManager | None = None,
    agent_store: OmnigentAgentStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/omnigent", tags=["omnigent"])
    manager = manager or OmnigentManager()
    native_manager = native_manager or NativeOmnigentManager()
    agent_store = agent_store or OmnigentAgentStore()
```

(c) Add these routes before `return router`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_omnigent_routes.py -k agents_crud -v`
Expected: PASS

- [ ] **Step 5: Commit** (per Commit policy)

```bash
git add routes/omnigent_routes.py tests/test_omnigent_routes.py
git commit -m "feat(omnigent): agents CRUD API"
```

---

## Task 5: API — orchestrator, model-options, compile

**Files:**
- Modify: `routes/omnigent_routes.py`
- Test: `tests/test_omnigent_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_omnigent_routes.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_omnigent_routes.py -k orchestrator_and_compile -v`
Expected: FAIL with `AssertionError: GET /api/omnigent/orchestrator not found`

- [ ] **Step 3: Write minimal implementation**

In `routes/omnigent_routes.py`, add before `return router`:
```python
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
```

(Note: `owner_filter` and `get_current_user` are already imported at the top of this file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_omnigent_routes.py -v`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Commit** (per Commit policy)

```bash
git add routes/omnigent_routes.py tests/test_omnigent_routes.py
git commit -m "feat(omnigent): orchestrator, model-options, compile API"
```

---

## Task 6: Frontend — state + Orchestrator control

**Files:**
- Modify: `static/js/omnigent.js`

- [ ] **Step 1: Extend state + fetch**

In `static/js/omnigent.js`, add to `_state` (after `presets: []`):
```javascript
  agents: [],
  orchestrator: { backend: 'claude-subscription', model: null, workspace: '' },
  modelEndpoints: [],
```

In `refresh()`, extend the `Promise.all` array and assignments:
```javascript
    fetchJson('/api/omnigent/agents', { agents: [] }),
    fetchJson('/api/omnigent/orchestrator', { backend: 'claude-subscription', model: null, workspace: '' }),
    fetchJson('/api/omnigent/model-options', { endpoints: [] }),
```
Capture them (rename the destructure to include the three new vars) and set:
```javascript
  _state.agents = Array.isArray(agentPayload.agents) ? agentPayload.agents : [];
  _state.orchestrator = orchestratorPayload || _state.orchestrator;
  _state.modelEndpoints = Array.isArray(modelPayload.endpoints) ? modelPayload.endpoints : [];
```

- [ ] **Step 2: Add the Orchestrator control render + helpers**

Add these functions (above `render()`):
```javascript
const BACKEND_LABELS = {
  'claude-subscription': 'Claude (subscription)',
  'chatgpt-subscription': 'ChatGPT (subscription)',
  'api': 'API endpoint',
};

function backendOptions(selected) {
  return Object.entries(BACKEND_LABELS).map(([id, label]) =>
    `<option value="${id}" ${id === selected ? 'selected' : ''}>${esc(label)}</option>`).join('');
}

function modelOptions(selected) {
  const opts = ['<option value="">— pick a model —</option>'];
  for (const ep of _state.modelEndpoints) {
    for (const m of ep.models || []) {
      const val = `${ep.id}::${m}`;
      opts.push(`<option value="${esc(val)}" ${val === selected ? 'selected' : ''}>${esc(ep.name)} · ${esc(m)}</option>`);
    }
  }
  return opts.join('');
}

function orchModelValue() {
  const m = _state.orchestrator?.model;
  return m ? `${m.endpoint_id}::${m.model}` : '';
}

function renderOrchestrator() {
  const o = _state.orchestrator || {};
  const isApi = o.backend === 'api';
  return `
    <section class="omnigent-section">
      <div class="omnigent-section-head"><h4>Orchestrator</h4><span>Coordinates the crew</span></div>
      <div class="omnigent-orch-row">
        <select id="omnigent-orch-backend" class="omnigent-select">${backendOptions(o.backend)}</select>
        <select id="omnigent-orch-model" class="omnigent-select" ${isApi ? '' : 'style="display:none"'}>${modelOptions(orchModelValue())}</select>
        <input id="omnigent-orch-workspace" class="omnigent-input" placeholder="Workspace dir (optional)" value="${esc(o.workspace || '')}">
        <button class="admin-btn-sm" data-omnigent-action="save-orchestrator">Save</button>
      </div>
    </section>`;
}
```

- [ ] **Step 3: Insert into `render()` + wire save**

In `render()`, insert `${renderOrchestrator()}` right after the hero `</section>` and before the Goal section.

In `wireActions()`, add branches:
```javascript
      else if (action === 'save-orchestrator') await saveOrchestrator();
```
And add the handler (above `wireActions`):
```javascript
async function saveOrchestrator() {
  const toast = window.uiModule?.showToast;
  const backend = document.getElementById('omnigent-orch-backend')?.value || 'claude-subscription';
  const workspace = document.getElementById('omnigent-orch-workspace')?.value || '';
  let model = null;
  const raw = document.getElementById('omnigent-orch-model')?.value || '';
  if (backend === 'api' && raw.includes('::')) {
    const [endpoint_id, m] = raw.split('::');
    model = { endpoint_id, model: m };
  }
  try {
    _state.orchestrator = await putJson('/api/omnigent/orchestrator', { backend, model, workspace });
    toast?.('Orchestrator saved');
    render();
  } catch (err) { toast?.(err?.message || 'Save failed'); }
}
```
Add a `putJson` helper next to `postJson`:
```javascript
async function putJson(url, payload = {}) {
  const res = await fetch(url, { method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || 'Request failed');
  return data;
}
```
Add a change-listener so the model dropdown toggles with backend: in `wireActions`, after the loop, add:
```javascript
  const ob = root.querySelector('#omnigent-orch-backend');
  if (ob && !ob.dataset.bound) {
    ob.dataset.bound = '1';
    ob.addEventListener('change', () => {
      const m = root.querySelector('#omnigent-orch-model');
      if (m) m.style.display = ob.value === 'api' ? '' : 'none';
    });
  }
```

- [ ] **Step 4: Manual smoke (after deploy in Task 9)** — orchestrator backend/model/workspace persists across refresh.

- [ ] **Step 5: Commit** (per Commit policy)

```bash
git add static/js/omnigent.js
git commit -m "feat(omnigent): orchestrator control in UI"
```

---

## Task 7: Frontend — Agents section + add/edit/delete

**Files:**
- Modify: `static/js/omnigent.js`

- [ ] **Step 1: Add agent rendering + form**

Add (above `render()`):
```javascript
function renderAgentRow(a) {
  const model = a.model?.model ? ` · ${esc(a.model.model)}` : '';
  return `
    <div class="omnigent-agent-row" data-agent-id="${esc(a.id)}">
      <div class="omnigent-agent-main">
        <span class="omnigent-agent-name">${esc(a.name)}</span>
        <small>${esc(BACKEND_LABELS[a.backend] || a.backend)}${model}</small>
        <p>${esc(a.role || '')}</p>
      </div>
      <span class="omnigent-worker-status ${a.enabled ? 'ready' : 'cancelled'}">${a.enabled ? 'On' : 'Off'}</span>
      <div class="omnigent-agent-actions">
        <button class="omnigent-link-btn" data-omnigent-action="edit-agent" data-agent-id="${esc(a.id)}">Edit</button>
        <button class="omnigent-link-btn" data-omnigent-action="delete-agent" data-agent-id="${esc(a.id)}">Delete</button>
      </div>
    </div>`;
}

function renderAgents() {
  const rows = _state.agents.map(renderAgentRow).join('') ||
    '<div class="omnigent-empty">No agents yet. Add one to build your crew.</div>';
  return `
    <section class="omnigent-section">
      <div class="omnigent-section-head">
        <h4>Agents</h4>
        <button class="admin-btn-add" data-omnigent-action="add-agent">Add agent</button>
      </div>
      <div class="omnigent-agent-list">${rows}</div>
    </section>`;
}
```

- [ ] **Step 2: Wire the agent modal (prompt-based form, no new HTML file)**

Add handlers (above `wireActions`):
```javascript
function agentModelValue(a) {
  return a?.model ? `${a.model.endpoint_id}::${a.model.model}` : '';
}

async function openAgentForm(existing) {
  const isEdit = !!existing;
  const name = window.prompt('Agent name', existing?.name || '');
  if (name === null) return;
  const role = window.prompt('Role / instructions', existing?.role || '') || '';
  const backend = window.prompt(
    'Backend: claude-subscription | chatgpt-subscription | api',
    existing?.backend || 'claude-subscription') || 'claude-subscription';
  let model = null;
  if (backend === 'api') {
    const choices = _state.modelEndpoints.flatMap(ep => (ep.models || []).map(m => `${ep.id}::${m}`));
    const picked = window.prompt('Model (endpointId::model)\n' + choices.join('\n'),
      agentModelValue(existing)) || '';
    if (picked.includes('::')) {
      const [endpoint_id, m] = picked.split('::');
      model = { endpoint_id, model: m };
    }
  }
  const payload = { name, role, backend, model, enabled: existing ? existing.enabled : true };
  const toast = window.uiModule?.showToast;
  try {
    if (isEdit) await putJson(`/api/omnigent/agents/${encodeURIComponent(existing.id)}`, payload);
    else await postJson('/api/omnigent/agents', payload);
    toast?.(isEdit ? 'Agent updated' : 'Agent added');
    await refresh();
  } catch (err) { toast?.(err?.message || 'Save failed'); }
}

async function deleteAgent(id) {
  const toast = window.uiModule?.showToast;
  if (!window.confirm('Delete this agent?')) return;
  try {
    await fetch(`/api/omnigent/agents/${encodeURIComponent(id)}`,
      { method: 'DELETE', credentials: 'same-origin' });
    toast?.('Agent deleted');
    await refresh();
  } catch (err) { toast?.(err?.message || 'Delete failed'); }
}
```
> Note: a `window.prompt` form keeps this task small and dependency-free. If a richer modal is wanted, follow up by reusing `modalManager.js` — out of scope for v1.

In `wireActions`, add branches:
```javascript
      else if (action === 'add-agent') await openAgentForm(null);
      else if (action === 'edit-agent') await openAgentForm(_state.agents.find(a => a.id === target.dataset.agentId));
      else if (action === 'delete-agent') await deleteAgent(target.dataset.agentId || '');
```

- [ ] **Step 3: Insert `${renderAgents()}` into `render()`** — replace the existing "Worker roster" section block with `${renderAgents()}`.

- [ ] **Step 4: Commit** (per Commit policy)

```bash
git add static/js/omnigent.js
git commit -m "feat(omnigent): agents add/edit/delete in UI"
```

---

## Task 8: Frontend — View run config + static test

**Files:**
- Modify: `static/js/omnigent.js`
- Test: `tests/test_omnigent_static.py`

- [ ] **Step 1: Write the failing static test**

Append to `tests/test_omnigent_static.py` (match the file's existing read-and-assert style):
```python
def test_omnigent_js_exposes_agent_and_orchestrator_controls():
    from pathlib import Path
    js = Path("static/js/omnigent.js").read_text(encoding="utf-8")
    assert "renderAgents" in js
    assert "renderOrchestrator" in js
    assert "/api/omnigent/agents" in js
    assert "/api/omnigent/orchestrator" in js
    assert "/api/omnigent/agents/compile" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_omnigent_static.py -k agent_and_orchestrator -v`
Expected: FAIL (compile reference not present yet)

- [ ] **Step 3: Add "View run config" action**

Add handler (above `wireActions`):
```javascript
async function viewRunConfig() {
  const toast = window.uiModule?.showToast;
  try {
    const data = await fetchJson('/api/omnigent/agents/compile', { yaml: '' });
    const blob = new Blob([data.yaml || ''], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (err) { toast?.(err?.message || 'Compile failed'); }
}
```
In `renderAgents()`'s section head, add next to "Add agent":
```javascript
        <button class="admin-btn-sm" data-omnigent-action="view-config">View run config</button>
```
In `wireActions`, add:
```javascript
      else if (action === 'view-config') await viewRunConfig();
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_omnigent_static.py -k agent_and_orchestrator -v`
Expected: PASS

- [ ] **Step 5: Commit** (per Commit policy)

```bash
git add static/js/omnigent.js tests/test_omnigent_static.py
git commit -m "feat(omnigent): view/download compiled run config"
```

---

## Task 9: Build, test, deploy, verify

**Files:** none (ops)

- [ ] **Step 1: Run the full omnigent test suite locally**

Run: `pytest tests/test_omnigent_agents.py tests/test_omnigent_routes.py tests/test_omnigent_static.py -v`
Expected: all PASS.

- [ ] **Step 2: Build the image from the checkout**

```powershell
docker compose -p odysseus -f "C:\Users\Dinis Mira~\odysseus\docker-compose.yml" build odysseus
```
Expected: exit 0.

- [ ] **Step 3: Run tests inside the image (tests are .dockerignored — mount them)**

```powershell
docker run --rm --entrypoint python -v "C:\Users\Dinis Mira~\odysseus\tests:/app/tests" odysseus-odysseus -m pytest /app/tests/test_omnigent_agents.py /app/tests/test_omnigent_routes.py -v
```
Expected: all PASS (confirms PyYAML present in image).

- [ ] **Step 4: Recreate the container on real data**

```powershell
docker compose -p odysseus --project-directory "C:\Users\Dinis Mira~\odysseus" -f "C:\Users\Dinis Mira~\odysseus\docker-compose.yml" up -d --no-build odysseus
```
Then poll `http://127.0.0.1:7000/api/health` until 200.

- [ ] **Step 5: Smoke-test the new endpoints (authenticated via browser cookie or in-container TestClient)**

In-container quick check:
```bash
docker exec -i odysseus-odysseus-1 python - <<'EOF'
from starlette.testclient import TestClient
import app as A
# NOTE: routes require auth; this asserts they are registered (401), not 404.
c = TestClient(A.app)
for path in ("/api/omnigent/agents", "/api/omnigent/orchestrator", "/api/omnigent/agents/compile", "/api/omnigent/model-options"):
    r = c.get(path)
    print(path, r.status_code)
    assert r.status_code in (200, 401), r.status_code
print("routes registered OK")
EOF
```
Expected: each path prints `200` or `401` (registered), then `routes registered OK`.

- [ ] **Step 6: Manual UI verification (hard-refresh the app)**

- Open Omnigent modal → Orchestrator control shows; set backend=API → model dropdown appears; Save persists across Refresh.
- Add agent (each backend); it appears in the Agents list; Edit toggles; Delete removes.
- "View run config" opens valid YAML reflecting your agents + orchestrator harness.

- [ ] **Step 7: Commit** (per Commit policy) — only if any ops fixups were needed.

---

## Self-Review (completed during planning)

- **Spec coverage:** R1 persist→T1; R2 backends→T1/T4; R3 API model from ModelEndpoint→T5 model-options + T6/T7 dropdowns; R4 orchestrator model→T2/T5/T6; R5 Claude-Code env/tools→T3 (harness map + reused canonical tools + workspace); R6 compile→T3/T5/T8; R7 saved agents drive runs→T7 (roster replaced) + future native exec noted. ✓
- **Placeholder scan:** none — all steps include real code. The `window.prompt` agent form is an explicit, justified v1 simplification (richer modal noted as out-of-scope follow-up). ✓
- **Type consistency:** store methods (`create_agent/list_agents/get_agent/update_agent/delete_agent/get_orchestrator/set_orchestrator`) and `compile_config(agents, orchestrator)` signatures match across Tasks 1–5; JS `BACKEND_LABELS`, `putJson`, `renderOrchestrator/renderAgents` referenced consistently across Tasks 6–8. ✓
