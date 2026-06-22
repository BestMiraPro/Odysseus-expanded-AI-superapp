"""Native Omnigent crew manager tests."""

from __future__ import annotations


def test_native_manager_creates_owner_scoped_run(tmp_path):
    from src.omnigent_native import NativeOmnigentManager

    manager = NativeOmnigentManager(state_path=tmp_path / "runs.json")
    run = manager.create_run(owner="alice", goal="Ship the feature", preset="build")

    assert run["owner"] == "alice"
    assert run["goal"] == "Ship the feature"
    assert run["preset"] == "build"
    assert run["status"] == "draft"
    assert [worker["id"] for worker in run["workers"]] == [
        "architect",
        "coder",
        "reviewer",
        "executor",
    ]
    assert manager.list_runs("bob") == []
    assert manager.list_runs("alice")[0]["id"] == run["id"]


def test_native_manager_start_and_cancel_are_owner_scoped(tmp_path):
    from src.omnigent_native import NativeOmnigentManager

    manager = NativeOmnigentManager(state_path=tmp_path / "runs.json")
    run = manager.create_run(owner="alice", goal="Review the release", preset="review")

    started = manager.start_run(run["id"], owner="alice")

    assert started["status"] == "completed"
    assert started["summary"]
    assert all(worker["status"] == "completed" for worker in started["workers"])
    assert len(started["timeline"]) >= 4
    assert manager.get_run(run["id"], owner="bob") is None

    cancelled = manager.cancel_run(run["id"], owner="alice")
    assert cancelled["status"] == "cancelled"
    assert cancelled["timeline"][-1]["kind"] == "cancelled"


def test_native_manager_rejects_empty_goal(tmp_path):
    from src.omnigent_native import NativeOmnigentManager

    manager = NativeOmnigentManager(state_path=tmp_path / "runs.json")

    try:
        manager.create_run(owner="alice", goal=" ", preset="balanced")
    except ValueError as exc:
        assert "goal" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")
