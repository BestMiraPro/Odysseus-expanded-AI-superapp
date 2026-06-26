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

    # ---- orchestrator ---------------------------------------------------
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


# ---- Omnigent config compilation ----------------------------------------
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
