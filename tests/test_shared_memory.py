"""Shared memory repo: config, GitHub reads, caching, routes and chat prompts."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import shared_memory as sm
from src.chat_processor import ChatProcessor

_REAL_CLIENT = httpx.Client
PROFILE = "# Profile\n\n## How to respond\n- Short answers.\n- End with one action."


def _secrets():
    return importlib.import_module("src.secret_storage")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "CONFIG_FILE", str(tmp_path / "shared_memory.json"))
    # Other suites swap src.secret_storage in sys.modules; patch the module
    # shared_memory will import, not a stale package attribute.
    secrets = _secrets()
    monkeypatch.setattr(secrets, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secrets, "_fernet", None)
    sm._cache.clear()
    yield
    sm._cache.clear()


def _github(monkeypatch, files=None, status=200):
    """Answer GitHub contents API calls from ``files`` ({path: text}); records requests."""
    files = {"USER.md": PROFILE} if files is None else files
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status)
        path = request.url.path.split("/contents/", 1)[1]
        if path not in files:
            return httpx.Response(404)
        return httpx.Response(200, text=files[path])

    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


def _enable(owner="alice", **kw):
    return sm.save_config(owner, enabled=True, repo=kw.pop("repo", "someone/memory"), **kw)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["someone/memory", "https://github.com/someone/memory",
                                   "https://github.com/someone/memory.git", "git@github.com:someone/memory.git",
                                   "github.com/someone/memory/"])
def test_repo_accepts_names_and_github_urls(value):
    assert sm.parse_repo(value) == "someone/memory"


@pytest.mark.parametrize("value", ["", "memory", "a/b/c", "https://evil.example/someone/memory",
                                   "someone/..", "../memory", "some one/memory"])
def test_repo_rejects_anything_else(value):
    with pytest.raises(ValueError):
        sm.parse_repo(value)


def test_bad_paths_and_branches_are_rejected():
    with pytest.raises(ValueError):
        _enable(files=["../secrets.md"])
    with pytest.raises(ValueError):
        _enable(branch="main/../x")
    with pytest.raises(ValueError):
        sm.save_config("alice", enabled=True, repo="")


def test_token_is_encrypted_kept_and_never_returned():
    cfg = _enable(token="ghp_secret", files="USER.md, notes/AGENTS.md")
    assert cfg.files == ["USER.md", "notes/AGENTS.md"]
    stored = json.loads(open(sm.CONFIG_FILE, encoding="utf-8").read())["alice"]
    assert stored["token"].startswith("enc:") and "ghp_secret" not in json.dumps(stored)
    public = sm.load_config("alice").public()
    assert public["has_token"] is True and "token" not in public

    _enable(token=None)                         # untouched
    assert _secrets().decrypt(sm.load_config("alice").token) == "ghp_secret"
    _enable(token="")                           # removed
    assert sm.load_config("alice").token == ""


def test_config_is_per_user():
    _enable("alice")
    assert sm.load_config("bob").enabled is False
    assert sm.configured_owners() == ["alice"]


# ---------------------------------------------------------------------------
# reading GitHub
# ---------------------------------------------------------------------------

def test_fetch_reads_each_file_with_branch_and_token(monkeypatch):
    seen = _github(monkeypatch, {"USER.md": PROFILE, "notes/AGENTS.md": "agent rules"})
    cfg = _enable(branch="dev", files=["USER.md", "notes/AGENTS.md"], token="ghp_secret")
    files = sm.fetch(cfg)
    assert [(f.path, f.text) for f in files] == [("USER.md", PROFILE), ("notes/AGENTS.md", "agent rules")]
    assert [r.url.host for r in seen] == ["api.github.com", "api.github.com"]
    assert seen[0].url.path == "/repos/someone/memory/contents/USER.md"
    assert seen[0].url.params["ref"] == "dev"
    assert seen[0].headers["Authorization"] == "Bearer ghp_secret"


def test_fetch_explains_missing_files_and_refused_tokens(monkeypatch):
    _github(monkeypatch, {})
    with pytest.raises(sm.SharedMemoryError, match="add an access token"):
        sm.fetch(_enable())
    _github(monkeypatch, status=401)
    with pytest.raises(sm.SharedMemoryError, match="access token"):
        sm.fetch(_enable(token="bad"))


def test_long_files_are_cut_at_the_limit(monkeypatch):
    monkeypatch.setattr(sm, "MAX_FILE_CHARS", 100)
    _github(monkeypatch, {"USER.md": "x" * 500})
    (f,) = sm.fetch(_enable())
    assert len(f.text) == 100 and f.truncated
    assert "rest of this file not loaded" in (sm.refresh("alice") and sm.context_text("alice"))


def test_a_failed_reload_keeps_the_last_good_files(monkeypatch):
    _github(monkeypatch)
    _enable()
    files, error = sm.refresh("alice")
    assert error is None and files[0].text == PROFILE
    _github(monkeypatch, status=503)
    files, error = sm.refresh("alice")
    assert "503" in error and files[0].text == PROFILE
    assert sm.status("alice")["error"] and sm.files_for("alice")[0].text == PROFILE


def test_cached_reads_do_not_touch_the_network(monkeypatch):
    seen = _github(monkeypatch)
    _enable()
    sm.refresh("alice")
    for _ in range(3):
        assert sm.files_for("alice")[0].text == PROFILE
    assert len(seen) == 1


def test_changing_the_config_drops_the_cache(monkeypatch):
    _github(monkeypatch, {"USER.md": PROFILE, "OTHER.md": "other"})
    _enable()
    sm.refresh("alice")
    _enable(files=["OTHER.md"])
    assert sm.files_for("alice", wait=True)[0].text == "other"


def test_context_text_frames_the_files_as_read_only_instructions(monkeypatch):
    _github(monkeypatch)
    _enable()
    assert sm.context_text("alice") == ""           # nothing read yet; never blocks
    text = sm.context_text("alice", wait=True)
    assert "someone/memory" in text and "standing instructions" in text and "read-only" in text
    assert "=== USER.md ===\n" + PROFILE in text
    sm.save_config("alice", enabled=False, repo="someone/memory")
    assert sm.context_text("alice", wait=True) == ""


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from routes.shared_memory_routes import setup_shared_memory_routes

    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user", "alice")
        return await call_next(request)

    app.include_router(setup_shared_memory_routes())
    return TestClient(app)


def test_routes_save_read_and_report_without_leaking_the_token(client, monkeypatch):
    _github(monkeypatch)
    resp = client.put("/api/shared-memory", json={"enabled": True, "repo": "https://github.com/someone/memory",
                                                  "files": "USER.md", "token": "ghp_secret"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"]["repo"] == "someone/memory" and data["config"]["has_token"] is True
    assert data["status"]["files"] == [{"path": "USER.md", "chars": len(PROFILE), "truncated": False}]
    assert "ghp_secret" not in resp.text and "enc:" not in resp.text
    assert client.get("/api/shared-memory").json()["config"]["has_token"] is True
    assert client.get("/api/shared-memory", headers={"x-user": "bob"}).json()["config"]["enabled"] is False
    assert client.post("/api/shared-memory/refresh").json()["status"]["error"] is None


def test_routes_reject_a_bad_repository(client):
    resp = client.put("/api/shared-memory", json={"enabled": True, "repo": "https://evil.example/a/b"})
    assert resp.status_code == 400 and "GitHub repository" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

class _Memory:
    def load(self, owner=None):
        return []


def _preface(**kw):
    processor = ChatProcessor(memory_manager=_Memory(), personal_docs_manager=SimpleNamespace(rag_manager=None))
    preface, _, _ = processor.build_context_preface(message="hi", session=SimpleNamespace(), use_rag=False,
                                                    owner="alice", **kw)
    return preface


def test_chat_gets_the_shared_memory_as_a_system_message(monkeypatch):
    _github(monkeypatch)
    _enable()
    sm.refresh("alice")
    shared = [m for m in _preface(use_memory=True) if PROFILE in m["content"]]
    assert len(shared) == 1 and shared[0]["role"] == "system"
    # Casual messages skip saved-memory recall but keep the standing instructions.
    assert any(PROFILE in m["content"] for m in _preface(use_memory=False, use_shared_memory=True))
    assert not any(PROFILE in m["content"] for m in _preface(use_memory=False))
    assert not any(PROFILE in m["content"] for m in _preface(use_memory=True, incognito=True))


def test_stale_files_are_served_at_once_while_a_refresh_runs(monkeypatch):
    seen = _github(monkeypatch)
    _enable()
    sm.refresh("alice")
    fp, _, files, error = sm._cache["alice"]
    sm._cache["alice"] = (fp, 0.0, files, error)          # expired
    started = []
    monkeypatch.setattr(sm, "_refresh_in_background", started.append)
    assert sm.files_for("alice", wait=True)[0].text == PROFILE
    assert started == ["alice"] and len(seen) == 1         # no blocking read
