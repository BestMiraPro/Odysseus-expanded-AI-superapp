"""Memory transfer: export gating, URL handling, and the pull merge.

The pull must re-own rows to the pulling user (the source's usernames mean
nothing here), reuse the file importer's dedup, and write nothing when the
source returns something that is not a transfer payload.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.memory_transfer_routes as mt


def _endpoint(router, path, method):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise AssertionError(path)


def _stores(memories=(), skills=()):
    saved = {"memories": None, "skills": []}
    mem = MagicMock()
    mem.load.side_effect = lambda owner=None: [m for m in memories if owner is None or m.get("owner") == owner]
    mem.load_all_for_update.return_value = list(memories)
    mem.save.side_effect = lambda rows: saved.__setitem__("memories", rows)
    sk = MagicMock()
    sk.load.side_effect = lambda owner=None: [s for s in skills if owner is None or s.get("owner") == owner]
    sk.load_all.return_value = list(skills)
    sk.add_skill.side_effect = lambda **kw: saved["skills"].append(kw) or {"id": kw["title"], "name": kw.get("name")}
    return mem, sk, saved


def _req(**state):
    return SimpleNamespace(state=SimpleNamespace(**state), app=SimpleNamespace(state=SimpleNamespace()))


# ── export ──

def test_export_with_token_returns_only_token_owner_rows():
    mem, sk, _ = _stores(
        memories=[{"text": "a", "owner": "dinis"}, {"text": "b", "owner": "other"}],
        skills=[{"title": "s", "owner": "dinis"}],
    )
    router = mt.setup_memory_transfer_routes(mem, sk)
    export = _endpoint(router, "/api/memory-transfer/export", "GET")
    req = _req(api_token=True, api_token_owner="dinis", api_token_scopes=["memory:read"], current_user="api")
    out = export(req, include_skills=True)
    assert out["kind"] == mt.TRANSFER_KIND
    assert [m["text"] for m in out["memories"]] == ["a"]
    assert [s["title"] for s in out["skills"]] == ["s"]


def test_export_rejects_token_without_memory_scope():
    mem, sk, _ = _stores()
    export = _endpoint(mt.setup_memory_transfer_routes(mem, sk), "/api/memory-transfer/export", "GET")
    req = _req(api_token=True, api_token_owner="dinis", api_token_scopes=["chat"], current_user="api")
    with pytest.raises(HTTPException) as e:
        export(req, include_skills=True)
    assert e.value.status_code == 403


def test_export_can_leave_skills_out():
    mem, sk, _ = _stores(skills=[{"title": "s", "owner": "dinis"}])
    export = _endpoint(mt.setup_memory_transfer_routes(mem, sk), "/api/memory-transfer/export", "GET")
    req = _req(api_token=True, api_token_owner="dinis", api_token_scopes=["memory:write"], current_user="api")
    assert "skills" not in export(req, include_skills=False)


# ── source URL ──

@pytest.mark.parametrize("raw,expected", [
    ("192.168.1.20:7000", "http://192.168.1.20:7000/api/memory-transfer/export"),
    ("https://ody.example.com/", "https://ody.example.com/api/memory-transfer/export"),
    ("https://ody.example.com/sub", "https://ody.example.com/sub/api/memory-transfer/export"),
    ("http://h:7000/api/memory-transfer/export", "http://h:7000/api/memory-transfer/export"),
])
def test_source_url_normalised(raw, expected):
    assert mt._source_export_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://h", "http://u:p@h", "http://h/?x=1", None])
def test_source_url_rejected(raw):
    with pytest.raises(HTTPException):
        mt._source_export_url(raw)


# ── pull ──

def _pull(monkeypatch, mem, sk, payload, body=None, user="admin"):
    monkeypatch.setattr(mt, "require_admin", lambda request: None)
    monkeypatch.setattr(mt, "get_current_user", lambda request: user)
    seen = {}

    async def fake_fetch(url, token, include_skills):
        seen.update(url=url, token=token, include_skills=include_skills)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(mt, "_fetch_export", fake_fetch)
    pull = _endpoint(mt.setup_memory_transfer_routes(mem, sk), "/api/memory-transfer/pull", "POST")
    body = body or {"source_url": "10.0.0.5:7000", "token": "ody_abc"}
    return asyncio.run(pull(_req(), body)), seen


def _payload(memories=(), skills=()):
    return {"kind": mt.TRANSFER_KIND, "version": 1, "memories": list(memories), "skills": list(skills)}


def test_pull_reowns_and_dedups(monkeypatch):
    mem, sk, saved = _stores(memories=[{"text": "Lives in Oeiras", "owner": "admin"}])
    payload = _payload(
        memories=[{"text": "lives in oeiras", "owner": "dinis"}, {"text": "Trains 3x/week", "owner": "dinis"}],
        skills=[{"title": "Deploy", "owner": "dinis"}],
    )
    out, seen = _pull(monkeypatch, mem, sk, payload)
    assert seen["url"] == "http://10.0.0.5:7000/api/memory-transfer/export"
    assert out["sections"][0] == {"name": "memories", "added": 1, "skipped": 1}
    assert out["sections"][1] == {"name": "skills", "added": 1, "skipped": 0}
    new = [m for m in saved["memories"] if m["text"] == "Trains 3x/week"]
    assert new and new[0]["owner"] == "admin"
    assert saved["skills"][0]["owner"] == "admin"
    # The payload the source sent is not mutated.
    assert payload["memories"][1]["owner"] == "dinis"


def test_pull_dry_run_writes_nothing(monkeypatch):
    mem, sk, saved = _stores()
    out, _ = _pull(monkeypatch, mem, sk, _payload(memories=[{"text": "x"}]),
                   body={"source_url": "h", "token": "ody_abc", "dry_run": True})
    assert out["dry_run"] and out["memories"] == 1
    mem.save.assert_not_called()
    assert saved["skills"] == []


def test_pull_without_skills_ignores_them(monkeypatch):
    mem, sk, saved = _stores()
    out, seen = _pull(monkeypatch, mem, sk, _payload(skills=[{"title": "s"}]),
                      body={"source_url": "h", "token": "ody_abc", "include_skills": False})
    assert seen["include_skills"] is False
    assert saved["skills"] == []


def test_pull_malformed_rows_write_nothing(monkeypatch):
    mem, sk, _ = _stores()
    with pytest.raises(HTTPException) as e:
        _pull(monkeypatch, mem, sk, _payload(memories=[{"text": 123}]))
    assert e.value.status_code == 400
    mem.save.assert_not_called()


def test_pull_requires_ody_token(monkeypatch):
    mem, sk, _ = _stores()
    with pytest.raises(HTTPException) as e:
        _pull(monkeypatch, mem, sk, _payload(), body={"source_url": "h", "token": "secret"})
    assert e.value.status_code == 400


def test_fetch_rejects_non_transfer_json(monkeypatch):
    import httpx

    def handler(request):
        assert request.headers["authorization"] == "Bearer ody_abc"
        return httpx.Response(200, json={"hello": "world"})

    real = httpx.AsyncClient
    monkeypatch.setattr(mt.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(HTTPException) as e:
        asyncio.run(mt._fetch_export("http://h/api/memory-transfer/export", "ody_abc", True))
    assert e.value.status_code == 502
    assert "world" not in e.value.detail


def test_fetch_does_not_follow_redirects(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(302, headers={"location": "http://evil/"})

    real = httpx.AsyncClient
    monkeypatch.setattr(mt.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(HTTPException) as e:
        asyncio.run(mt._fetch_export("http://h/api/memory-transfer/export", "ody_abc", True))
    assert "redirect" in e.value.detail


def test_pull_indexes_new_memories_only(monkeypatch):
    mem, sk, _ = _stores(memories=[{"text": "old", "owner": "admin"}])
    vec = MagicMock(healthy=True)
    monkeypatch.setattr(mt, "require_admin", lambda request: None)
    monkeypatch.setattr(mt, "get_current_user", lambda request: "admin")

    async def fake_fetch(url, token, include_skills):
        return _payload(memories=[{"text": "old"}, {"text": "new", "id": "m2"}, {"text": "no id"}])

    monkeypatch.setattr(mt, "_fetch_export", fake_fetch)
    pull = _endpoint(mt.setup_memory_transfer_routes(mem, sk, memory_vector=vec),
                     "/api/memory-transfer/pull", "POST")
    asyncio.run(pull(_req(), {"source_url": "h", "token": "ody_abc"}))
    indexed = [c.args for c in vec.add.call_args_list]
    assert [t for _, t in indexed] == ["new", "no id"]
    assert indexed[0][0] == "m2" and indexed[1][0]
