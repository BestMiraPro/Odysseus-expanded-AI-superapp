"""Mounted deployments must retain timeout and foreground-activity policies."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def middleware_results(tmp_path_factory):
    # Importing the orchestrator configures logging and application services;
    # a subprocess isolates those effects from the rest of the test suite.
    data_dir = tmp_path_factory.mktemp("mounted-middleware")
    env = os.environ.copy()
    env.update({
        "AUTH_ENABLED": "false",
        "BACKGROUND_TASK_FOREGROUND_GATE": "true",
        "CHROMADB_CONNECT_TIMEOUT": "0.01",
        "CHROMADB_HOST": "127.0.0.1",
        "CHROMADB_PORT": "9",
        "DATABASE_URL": f"sqlite:///{data_dir / 'app.db'}",
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "ODYSSEUS_DISABLE_MCP": "1",
        "OPENAI_API_KEY": "",
        "PYTHONPATH": str(ROOT),
        "PYTHONUTF8": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        # Zero cancels non-exempt requests deterministically without sleeping.
        "REQUEST_HARD_TIMEOUT": "0",
    })
    probe = textwrap.dedent("""
        import json
        from types import SimpleNamespace

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import app as app_module
        from src import interactive_gate

        stop_reasons = []

        async def stop_background(*, reason):
            stop_reasons.append(reason)

        # Background scheduler cancellation affects external jobs. Keep the
        # real activity gate, replacing only the cancellation boundary.
        app_module.task_scheduler = SimpleNamespace(
            stop_background_tasks_for_foreground=stop_background,
        )

        def case(middleware, root_path, route_path):
            app = FastAPI()
            app.add_middleware(middleware)
            stop_reasons.clear()

            async def endpoint():
                return {"active_requests": interactive_gate._ACTIVE_REQUESTS}

            app.add_api_route(route_path, endpoint)
            with TestClient(app, root_path=root_path) as client:
                response = client.get(root_path + route_path)
            return {"status": response.status_code, "body": response.json(),
                    "stops": list(stop_reasons)}

        timeout_paths = ["/api/chat", "/api/study/materials/m-1/notes", "/api/models"]
        activity_paths = ["/api/tasks/runs/recent", "/api/health", "/api/models"]
        results = {"timeout": {}, "activity": {}}
        for root_path in ("", "/odysseus"):
            results["timeout"][root_path] = {
                path: case(app_module._RequestTimeoutMiddleware, root_path, path)
                for path in timeout_paths
            }
            results["activity"][root_path] = {
                path: case(app_module._InteractiveActivityMiddleware, root_path, path)
                for path in activity_paths
            }
        print("RESULT=" + json.dumps(results))
    """)
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    line = next((line for line in result.stdout.splitlines() if line.startswith("RESULT=")), None)
    assert line is not None, result.stdout
    return json.loads(line.removeprefix("RESULT="))


@pytest.mark.parametrize("root_path", ["", "/odysseus"])
def test_mounted_long_running_routes_keep_timeout_exemptions(middleware_results, root_path):
    results = middleware_results["timeout"][root_path]
    assert results["/api/chat"]["status"] == 200
    assert results["/api/study/materials/m-1/notes"]["status"] == 200
    assert results["/api/models"]["status"] == 504


@pytest.mark.parametrize("root_path", ["", "/odysseus"])
def test_mounted_passive_polling_does_not_interrupt_background_work(middleware_results, root_path):
    results = middleware_results["activity"][root_path]
    for path in ("/api/tasks/runs/recent", "/api/health"):
        assert results[path] == {"status": 200, "body": {"active_requests": 0}, "stops": []}
    assert results["/api/models"] == {
        "status": 200,
        "body": {"active_requests": 1},
        "stops": ["foreground request GET /api/models"],
    }
