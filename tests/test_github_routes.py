"""GitHub connection routes: owner scoping, the admin check, and the token never leaving storage."""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import github_access as ga

_REAL_CLIENT = httpx.Client
TOKEN = "github_pat_11AAAAAAA0" + "b" * 12 + "_" + "c" * 59
BOB_TOKEN = "github_pat_11ZZZZZZZ0" + "d" * 12 + "_" + "e" * 59
LOGINS = {TOKEN: "BestMiraPro", BOB_TOKEN: "BobOnGitHub"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "CONFIG_FILE", str(tmp_path / "github_access.json"))
    secrets = importlib.import_module("src.secret_storage")
    monkeypatch.setattr(secrets, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secrets, "_fernet", None)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.github.com" and request.url.path == "/user"
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in LOGINS:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"login": LOGINS[token], "type": "User"})

    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


@pytest.fixture
def client(monkeypatch):
    from routes.github_routes import setup_github_routes

    # alice and bob are admins unless a test says otherwise.
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: owner in ("alice", "bob"))
    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user", "alice")
        return await call_next(request)

    app.include_router(setup_github_routes())
    return TestClient(app)


def test_connection_is_stored_reported_and_forgotten_per_user_without_the_token(client):
    assert client.get("/api/github/connection").json() == {"connected": False, "login": None}
    resp = client.put("/api/github/connection", json={"token": TOKEN})
    assert resp.status_code == 200 and resp.json() == {"connected": True, "login": "BestMiraPro"}
    stored = open(ga.CONFIG_FILE, encoding="utf-8").read()
    assert TOKEN not in stored and json.loads(stored)["alice"]["token"].startswith("enc:")

    responses = [resp, client.get("/api/github/connection")]
    assert responses[1].json() == {"connected": True, "login": "BestMiraPro"}
    # Another user sees and changes only their own record.
    responses.append(client.get("/api/github/connection", headers={"x-user": "bob"}))
    assert responses[-1].json() == {"connected": False, "login": None}
    responses.append(client.delete("/api/github/connection", headers={"x-user": "bob"}))
    assert responses[-1].json() == {"deleted": False}
    responses.append(client.put("/api/github/connection", headers={"x-user": "bob"}, json={"token": BOB_TOKEN}))
    assert responses[-1].json()["login"] == "BobOnGitHub"
    assert client.get("/api/github/connection").json()["login"] == "BestMiraPro"

    responses.append(client.delete("/api/github/connection"))
    assert responses[-1].json() == {"deleted": True}
    assert client.get("/api/github/connection").json() == {"connected": False, "login": None}
    assert client.get("/api/github/connection", headers={"x-user": "bob"}).json()["connected"] is True
    for r in responses:
        assert TOKEN not in r.text and BOB_TOKEN not in r.text and "enc:" not in r.text


@pytest.mark.parametrize("token,reason", [("ghp_" + "a" * 36, "classic"), ("gho_" + "a" * 36, "OAuth"),
                                          ("nonsense", "fine-grained")])
def test_put_refuses_tokens_that_are_not_fine_grained(client, isolated, token, reason):
    resp = client.put("/api/github/connection", json={"token": token})
    assert resp.status_code == 400 and reason in resp.json()["detail"]
    assert isolated == []                               # GitHub was never asked
    assert client.get("/api/github/connection").json()["connected"] is False


def test_put_refuses_a_token_github_rejects(client):
    resp = client.put("/api/github/connection", json={"token": "github_pat_" + "z" * 82})
    assert resp.status_code == 400 and "refused" in resp.json()["detail"]
    assert client.get("/api/github/connection").json()["connected"] is False


def test_non_admin_on_a_multi_user_install_cannot_store_or_remove_a_token(client, isolated):
    client.put("/api/github/connection", json={"token": TOKEN})
    carol = {"x-user": "carol"}
    resp = client.put("/api/github/connection", headers=carol, json={"token": BOB_TOKEN})
    assert resp.status_code == 403
    assert client.delete("/api/github/connection", headers=carol).status_code == 403
    assert len(isolated) == 1                           # only alice's token was checked with GitHub
    assert client.get("/api/github/connection", headers=carol).json() == {"connected": False, "login": None}
    assert client.get("/api/github/connection").json()["connected"] is True


def test_single_user_install_can_connect(monkeypatch):
    from routes.github_routes import setup_github_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")      # the real privilege check, single-user mode
    app = FastAPI()
    app.include_router(setup_github_routes())
    client = TestClient(app)
    assert client.put("/api/github/connection", json={"token": TOKEN}).json() == {
        "connected": True, "login": "BestMiraPro"}
    assert set(json.loads(open(ga.CONFIG_FILE, encoding="utf-8").read())) == {""}
    assert client.delete("/api/github/connection").json() == {"deleted": True}


def test_the_app_registers_the_github_routes():
    from pathlib import Path

    app_source = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "from routes.github_routes import setup_github_routes" in app_source
    assert "app.include_router(setup_github_routes())" in app_source


def test_api_tokens_cannot_reach_the_connection_routes(monkeypatch):
    from routes.github_routes import setup_github_routes

    app = FastAPI()

    @app.middleware("http")
    async def _api_token(request: Request, call_next):
        request.state.current_user = "api"
        request.state.api_token = True
        request.state.api_token_owner = "alice"
        return await call_next(request)

    app.include_router(setup_github_routes())
    client = TestClient(app)
    assert client.get("/api/github/connection").status_code == 403
    assert client.put("/api/github/connection", json={"token": TOKEN}).status_code == 403
