# Native Omnigent Crew Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a native Odysseus crew workspace behind the existing Omnigent entry point, with no external Omnigent CLI required for the default experience.

**Architecture:** Add a focused native crew manager that persists owner-scoped run state to `data/omnigent_runs.json`, expose it through the existing `/api/omnigent/*` routes, and replace the modal's install-first UI with a native goal composer, presets, workers, timeline, and run list. Keep the old external bridge endpoints available as advanced compatibility.

**Tech Stack:** FastAPI route handlers, Python JSON state manager, existing Odysseus auth helpers, vanilla JS modal module, pytest.

---

### Task 1: Native Crew Manager Tests

**Files:**
- Create: `tests/test_omnigent_native.py`
- Create later: `src/omnigent_native.py`

- [x] **Step 1: Write failing tests**

```python
from pathlib import Path


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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_omnigent_native.py -q`

Expected: fails because `src.omnigent_native` does not exist.

### Task 2: Native Crew Manager Implementation

**Files:**
- Create: `src/omnigent_native.py`

- [x] **Step 1: Implement minimal manager**

Create presets, atomic JSON load/save, `create_run`, `list_runs`, `get_run`, `start_run`, `cancel_run`, and `status`.

- [x] **Step 2: Run manager tests**

Run: `pytest tests/test_omnigent_native.py -q`

Expected: all tests pass.

### Task 3: Route Tests

**Files:**
- Modify: `tests/test_omnigent_routes.py`
- Modify later: `routes/omnigent_routes.py`

- [x] **Step 1: Add failing route tests**

Add tests that:

- `GET /api/omnigent/status` includes `native.available == True`.
- `GET /api/omnigent/workers` returns native worker ids.
- `POST /api/omnigent/runs` creates an owner-scoped run.
- `POST /api/omnigent/runs/{run_id}/start` completes the run.
- `GET /api/omnigent/sessions` returns native runs.

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_omnigent_routes.py -q`

Expected: new tests fail because routes are missing native run endpoints.

### Task 4: Route Implementation

**Files:**
- Modify: `routes/omnigent_routes.py`

- [x] **Step 1: Inject native manager**

Update `setup_omnigent_routes` to accept `native_manager`, instantiate it by default, and add native data to status, sessions, workers, and capabilities.

- [x] **Step 2: Add native run endpoints**

Add owner-scoped create/get/start/cancel handlers. Use `get_current_user` for owner and `require_authenticated_request` for run mutation/read access.

- [x] **Step 3: Run route tests**

Run: `pytest tests/test_omnigent_routes.py tests/test_omnigent_native.py -q`

Expected: all tests pass.

### Task 5: Static UI Tests

**Files:**
- Modify: `tests/test_omnigent_static.py`
- Modify later: `static/js/omnigent.js`
- Modify later: `static/style.css`

- [x] **Step 1: Add failing static tests**

Assert `static/js/omnigent.js` contains native crew UI hooks:

- `data-omnigent-action="create-run"`
- `omnigent-goal-input`
- `Crew timeline`
- `/api/omnigent/runs`
- `Native crew`

Assert the old install-first phrasing is no longer primary.

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_omnigent_static.py -q`

Expected: fails until UI is replaced.

### Task 6: Frontend Implementation

**Files:**
- Modify: `static/js/omnigent.js`
- Modify: `static/style.css`

- [x] **Step 1: Replace modal content**

Render native crew shell with goal input, preset selector, worker roster, timeline, recent runs, and advanced bridge details.

- [x] **Step 2: Wire run actions**

Add client actions to create, start, cancel, refresh, select run, open settings, and download bridge bundle.

- [x] **Step 3: Run static tests**

Run: `pytest tests/test_omnigent_static.py -q`

Expected: all tests pass.

### Task 7: Focused Verification

**Files:**
- Test only

- [x] **Step 1: Run focused Omnigent tests**

Run: `pytest tests/test_omnigent_native.py tests/test_omnigent_routes.py tests/test_omnigent_static.py -q`

Expected: all tests pass.

Note: Local pytest execution was blocked because `pytest` is not installed in the available Python environment and `uv run` could not spawn it without installing dependencies. Verification was completed with Python compile checks, JavaScript syntax checks, and focused native/static smoke checks.

- [x] **Step 2: Check working tree**

Run: `git status --short`

Expected: only the intended native crew files are modified, plus the previously downloaded untracked OmniGen2 files.
