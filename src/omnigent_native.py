"""Native Odysseus crew workspace state for the Omnigent surface."""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.constants import DATA_DIR


STATE_FILE = Path(DATA_DIR) / "omnigent_runs.json"

WORKER_LIBRARY: dict[str, dict[str, str]] = {
    "architect": {
        "id": "architect",
        "label": "Architect",
        "source": "Odysseus native",
        "hint": "Frames the goal, risks, and execution strategy.",
    },
    "researcher": {
        "id": "researcher",
        "label": "Researcher",
        "source": "Odysseus native",
        "hint": "Finds context across tools, documents, and web-enabled sources.",
    },
    "coder": {
        "id": "coder",
        "label": "Coder",
        "source": "Odysseus native",
        "hint": "Turns the plan into implementation tasks.",
    },
    "reviewer": {
        "id": "reviewer",
        "label": "Reviewer",
        "source": "Odysseus native",
        "hint": "Checks quality, regressions, and missing tests.",
    },
    "executor": {
        "id": "executor",
        "label": "Executor",
        "source": "Odysseus native",
        "hint": "Carries out the next concrete action inside Odysseus.",
    },
}

PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "id": "balanced",
        "label": "Balanced",
        "description": "General-purpose crew for planning and doing.",
        "workers": ["architect", "researcher", "coder", "reviewer", "executor"],
    },
    "build": {
        "id": "build",
        "label": "Build",
        "description": "Product and code work with a review checkpoint.",
        "workers": ["architect", "coder", "reviewer", "executor"],
    },
    "research": {
        "id": "research",
        "label": "Research",
        "description": "Context gathering and synthesis.",
        "workers": ["architect", "researcher", "reviewer"],
    },
    "review": {
        "id": "review",
        "label": "Review",
        "description": "Risk-first review and next-step recommendations.",
        "workers": ["researcher", "reviewer", "executor"],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any) -> Any:
    return deepcopy(value)


class NativeOmnigentManager:
    """Persist and execute native crew runs.

    The first native version is intentionally deterministic. It establishes the
    owner-scoped run model and UI contract without requiring an uncontrolled
    background agent runner.
    """

    def __init__(self, state_path: str | os.PathLike[str] | None = None):
        self.state_path = Path(state_path) if state_path is not None else STATE_FILE

    def status(self, model_ready: bool = False) -> dict[str, Any]:
        return {
            "available": True,
            "mode": "native",
            "model_ready": bool(model_ready),
            "state_path": str(self.state_path),
            "presets": list(self.presets().values()),
        }

    def presets(self) -> dict[str, dict[str, Any]]:
        return _copy(PRESETS)

    def worker_roster(self) -> list[dict[str, Any]]:
        return [
            {
                **_copy(worker),
                "available": True,
                "status": "ready",
            }
            for worker in WORKER_LIBRARY.values()
        ]

    def create_run(self, owner: str | None, goal: str, preset: str = "balanced") -> dict[str, Any]:
        clean_goal = (goal or "").strip()
        if not clean_goal:
            raise ValueError("Crew goal is required")
        preset_id = preset if preset in PRESETS else "balanced"
        now = _now()
        run = {
            "id": f"crew-{uuid.uuid4().hex[:12]}",
            "owner": owner,
            "goal": clean_goal,
            "preset": preset_id,
            "preset_label": PRESETS[preset_id]["label"],
            "status": "draft",
            "summary": "",
            "workers": [self._new_worker(worker_id) for worker_id in PRESETS[preset_id]["workers"]],
            "timeline": [
                {
                    "kind": "created",
                    "label": "Crew drafted",
                    "detail": clean_goal,
                    "timestamp": now,
                }
            ],
            "created_at": now,
            "updated_at": now,
        }
        state = self._load_state()
        state["runs"].append(run)
        self._save_state(state)
        return _copy(run)

    def list_runs(self, owner: str | None) -> list[dict[str, Any]]:
        runs = [run for run in self._load_state()["runs"] if run.get("owner") == owner]
        runs.sort(key=lambda run: run.get("updated_at") or "", reverse=True)
        return _copy(runs)

    def sessions(self, owner: str | None) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "id": run["id"],
                    "title": run.get("goal") or "Crew run",
                    "status": run.get("status", "draft"),
                    "preset": run.get("preset"),
                    "updated_at": run.get("updated_at"),
                    "native": True,
                }
                for run in self.list_runs(owner)
            ]
        }

    def get_run(self, run_id: str, owner: str | None) -> dict[str, Any] | None:
        for run in self._load_state()["runs"]:
            if run.get("id") == run_id and run.get("owner") == owner:
                return _copy(run)
        return None

    def start_run(self, run_id: str, owner: str | None) -> dict[str, Any] | None:
        state = self._load_state()
        run = self._find_mutable_run(state, run_id, owner)
        if run is None:
            return None
        now = _now()
        run["status"] = "running"
        run["updated_at"] = now
        run["timeline"].append({
            "kind": "started",
            "label": "Crew started",
            "detail": run["preset_label"],
            "timestamp": now,
        })
        for worker in run["workers"]:
            worker["status"] = "completed"
            worker["current_step"] = "Completed native pass"
            worker["output"] = self._worker_output(worker, run["goal"])
            run["timeline"].append({
                "kind": "worker",
                "label": f"{worker['label']} completed",
                "detail": worker["output"],
                "timestamp": _now(),
                "worker_id": worker["id"],
            })
        run["status"] = "completed"
        run["summary"] = (
            f"Native crew completed '{run['goal']}' with "
            f"{', '.join(worker['label'] for worker in run['workers'])}. "
            "Open a chat, document, or task to continue from the recommended next action."
        )
        run["updated_at"] = _now()
        run["timeline"].append({
            "kind": "completed",
            "label": "Crew completed",
            "detail": run["summary"],
            "timestamp": run["updated_at"],
        })
        self._save_state(state)
        return _copy(run)

    def cancel_run(self, run_id: str, owner: str | None) -> dict[str, Any] | None:
        state = self._load_state()
        run = self._find_mutable_run(state, run_id, owner)
        if run is None:
            return None
        run["status"] = "cancelled"
        run["updated_at"] = _now()
        for worker in run["workers"]:
            if worker.get("status") in {"pending", "running"}:
                worker["status"] = "cancelled"
        run["timeline"].append({
            "kind": "cancelled",
            "label": "Crew cancelled",
            "detail": "The native crew run was cancelled.",
            "timestamp": run["updated_at"],
        })
        self._save_state(state)
        return _copy(run)

    def _new_worker(self, worker_id: str) -> dict[str, Any]:
        worker = _copy(WORKER_LIBRARY[worker_id])
        worker.update({
            "available": True,
            "status": "pending",
            "current_step": "",
            "output": "",
        })
        return worker

    def _worker_output(self, worker: dict[str, Any], goal: str) -> str:
        outputs = {
            "architect": f"Framed '{goal}' into a native Odysseus crew plan.",
            "researcher": f"Identified context needed before acting on '{goal}'.",
            "coder": f"Prepared implementation steps for '{goal}'.",
            "reviewer": f"Checked risks, tests, and verification needs for '{goal}'.",
            "executor": f"Recommended the next concrete Odysseus action for '{goal}'.",
        }
        return outputs.get(worker["id"], f"Completed worker pass for '{goal}'.")

    def _find_mutable_run(self, state: dict[str, Any], run_id: str, owner: str | None) -> dict[str, Any] | None:
        for run in state["runs"]:
            if run.get("id") == run_id and run.get("owner") == owner:
                return run
        return None

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"runs": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"runs": []}
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            return {"runs": []}
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.state_path)
