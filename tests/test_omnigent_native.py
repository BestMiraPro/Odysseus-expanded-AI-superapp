"""Native Omnigent crew metadata tests (run engine removed)."""

from __future__ import annotations


def test_status_reports_native_mode_and_presets(tmp_path):
    from src.omnigent_native import NativeOmnigentManager

    manager = NativeOmnigentManager(state_path=tmp_path / "runs.json")
    status = manager.status(model_ready=True)

    assert status["available"] is True
    assert status["mode"] == "native"
    assert status["model_ready"] is True
    preset_ids = {p["id"] for p in status["presets"]}
    assert {"balanced", "build", "research", "review"} <= preset_ids


def test_presets_and_worker_roster(tmp_path):
    from src.omnigent_native import NativeOmnigentManager

    manager = NativeOmnigentManager(state_path=tmp_path / "runs.json")

    assert manager.presets()["balanced"]["workers"][0] == "architect"
    roster_ids = {w["id"] for w in manager.worker_roster()}
    assert {"architect", "researcher", "coder", "reviewer", "executor"} <= roster_ids
    assert all(w["status"] == "ready" for w in manager.worker_roster())


def test_goal_gated_run_methods_are_removed():
    from src.omnigent_native import NativeOmnigentManager

    manager = NativeOmnigentManager()
    for removed in ("create_run", "start_run", "cancel_run", "list_runs", "get_run", "sessions"):
        assert not hasattr(manager, removed), f"{removed} should be removed"
