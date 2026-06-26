"""Native Odysseus crew metadata for the Omnigent surface.

The interactive, goal-gated crew runner was removed: Omnigent runs your
configured orchestrator + agents from a compiled config — there is no mandatory
goal. This module now only exposes the static crew presets / worker library and
a small status payload consumed by the Omnigent routes.
"""

from __future__ import annotations

import os
from copy import deepcopy
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


def _copy(value: Any) -> Any:
    return deepcopy(value)


class NativeOmnigentManager:
    """Expose the native crew presets / worker library and a status payload.

    No interactive run state: execution happens through Omnigent from a
    compiled config, so there is no goal-gated run lifecycle here anymore.
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
