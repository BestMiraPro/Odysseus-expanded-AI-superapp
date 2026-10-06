"""AI Council routes: roster, owner scoping, streaming ask, stop and persistence."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as dbmod
import routes.council_routes as cr
from core.database import Base, CouncilTurn, ModelEndpoint
from src import claude_subscription as cs


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(cr, "SessionLocal", Session)
    s = Session()
    s.add_all([
        ModelEndpoint(id="api1", name="DeepSeek", base_url="https://api.deepseek.com/v1", owner="alice",
                      is_enabled=True, model_type="llm", api_key="sk-1",
                      cached_models=json.dumps(["deepseek-v4", "text-embedding-3"])),
        ModelEndpoint(id="loc1", name="Ollama", base_url="http://127.0.0.1:11434/v1", owner="alice",
                      is_enabled=True, model_type="llm", cached_models=json.dumps(["qwen3:8b"])),
        ModelEndpoint(id="bob1", name="Bob's key", base_url="https://api.openai.com/v1", owner="bob",
                      is_enabled=True, model_type="llm", api_key="sk-bob", cached_models=json.dumps(["gpt-5.5"])),
        ModelEndpoint(id="off1", name="Disabled", base_url="https://api.openai.com/v1", owner="alice",
                      is_enabled=False, model_type="llm", cached_models=json.dumps(["gpt-5.5"])),
    ])
    s.commit()
    s.close()
    cr._RUNNING.clear()
    return Session


async def _fake_stream(url, model, messages, **kwargs):
    system = messages[0]["content"]
    if system == cr.council.REVIEW_SYSTEM:
        text = "ok\nFINAL RANKING:\n1. Response A\n2. Response B"
    elif system == cr.council.CHAIR_SYSTEM:
        text = "the council says 42"
    else:
        text = f"{model} thinks 42"
    yield f"data: {json.dumps({'delta': text})}\n\n"
    yield "data: [DONE]\n\n"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("src.llm_core.stream_llm", _fake_stream)
    monkeypatch.setattr("core.middleware.require_admin", lambda request: None)
    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user", "alice")
        return await call_next(request)

    app.include_router(cr.setup_council_routes())
    return TestClient(app)


def _events(resp_text: str):
    return [json.loads(line[5:]) for line in resp_text.splitlines() if line.startswith("data:")]


SEATS = {
    "members": [{"endpoint_id": "api1", "model": "deepseek-v4"}, {"endpoint_id": "loc1", "model": "qwen3:8b"}],
    "chairman": {"endpoint_id": "api1", "model": "deepseek-v4"},
    "mode": "full",
}


def test_roster_is_owner_scoped_and_classifies_endpoints(client, db):
    cs.provision("alice", cs.AUTH_MODE_TOKEN, "sk-ant-oat01-" + "b" * 40)
    data = client.get("/api/council/roster").json()
    by_id = {e["id"]: e for e in data["endpoints"]}
    assert set(by_id) == {"api1", "loc1", data["connections"]["claude"]["endpoint_id"]}
    assert by_id["api1"]["kind"] == "api"
    assert by_id["api1"]["models"] == ["deepseek-v4"]  # embedding model filtered out
    assert by_id["loc1"]["kind"] == "local"
    claude = by_id[data["connections"]["claude"]["endpoint_id"]]
    assert claude["kind"] == "subscription" and claude["provider"] == "claude-subscription"
    assert data["endpoints"][0]["kind"] == "subscription"  # subscriptions listed first
    assert data["connections"]["claude"]["connected"] is True
    assert data["connections"]["chatgpt"]["connected"] is False

    bob = client.get("/api/council/roster", headers={"x-user": "bob"}).json()
    assert [e["id"] for e in bob["endpoints"]] == ["bob1"]
    assert bob["connections"]["claude"]["connected"] is False


def test_ask_streams_all_stages_and_persists_the_turn(client, db):
    sid = client.post("/api/council/sessions", json={}).json()["id"]
    resp = client.post(f"/api/council/sessions/{sid}/ask", json={"question": "What is 6x7?", **SEATS})
    assert resp.status_code == 200
    events = _events(resp.text)
    assert events[0]["type"] == "turn"
    assert [m["model"] for m in events[0]["members"]] == ["deepseek-v4", "qwen3:8b"]
    assert "url" not in json.dumps(events[0]) and "sk-1" not in resp.text
    assert events[-1]["type"] == "done" and events[-1]["status"] == "done"
    assert events[-1]["final"] == "the council says 42"

    session = client.get(f"/api/council/sessions/{sid}").json()
    assert session["title"] == "What is 6x7?"
    assert session["config"]["members"] == SEATS["members"]
    (turn,) = session["turns"]
    assert turn["status"] == "done"
    assert turn["final"] == "the council says 42"
    assert [o["text"] for o in turn["opinions"]] == ["deepseek-v4 thinks 42", "qwen3:8b thinks 42"]
    assert len(turn["reviews"]) == 2 and len(turn["ranking"]) == 2

    # The next session starts from the last seats.
    new = client.post("/api/council/sessions", json={}).json()
    assert new["config"]["members"] == SEATS["members"]


def test_follow_up_turns_carry_history(client, db, monkeypatch):
    seen = []

    async def recording(url, model, messages, **kwargs):
        seen.append([m["content"] for m in messages])
        async for chunk in _fake_stream(url, model, messages, **kwargs):
            yield chunk

    monkeypatch.setattr("src.llm_core.stream_llm", recording)
    sid = client.post("/api/council/sessions", json={}).json()["id"]
    quick = {**SEATS, "mode": "quick"}
    client.post(f"/api/council/sessions/{sid}/ask", json={"question": "First?", **quick})
    seen.clear()
    client.post(f"/api/council/sessions/{sid}/ask", json={"question": "Second?", **quick})
    assert seen and all("First?" in contents and "the council says 42" in contents for contents in seen)


def test_sessions_are_private_to_their_owner(client, db):
    sid = client.post("/api/council/sessions", json={"title": "mine"}).json()["id"]
    assert client.get(f"/api/council/sessions/{sid}", headers={"x-user": "bob"}).status_code == 404
    assert client.post(f"/api/council/sessions/{sid}/ask", headers={"x-user": "bob"},
                       json={"question": "hi", **SEATS}).status_code == 404
    assert client.delete(f"/api/council/sessions/{sid}", headers={"x-user": "bob"}).status_code == 404
    assert [s["id"] for s in client.get("/api/council/sessions", headers={"x-user": "bob"}).json()["sessions"]] == []
    assert [s["id"] for s in client.get("/api/council/sessions").json()["sessions"]] == [sid]


@pytest.mark.parametrize("seat,status", [
    ({"endpoint_id": "bob1", "model": "gpt-5.5"}, 404),       # someone else's key
    ({"endpoint_id": "off1", "model": "gpt-5.5"}, 404),       # disabled endpoint
    ({"endpoint_id": "api1", "model": "not-enabled"}, 400),   # model not on the endpoint
    ({"endpoint_id": "api1", "model": "  "}, 400),
])
def test_ask_rejects_seats_the_user_cannot_use(client, db, seat, status):
    sid = client.post("/api/council/sessions", json={}).json()["id"]
    body = {"question": "hi", "members": [seat], "chairman": SEATS["chairman"]}
    assert client.post(f"/api/council/sessions/{sid}/ask", json=body).status_code == status
    s = db()
    try:
        assert s.query(CouncilTurn).count() == 0
    finally:
        s.close()


def test_ask_validates_question_and_seat_count(client, db):
    sid = client.post("/api/council/sessions", json={}).json()["id"]
    assert client.post(f"/api/council/sessions/{sid}/ask", json={"question": "  ", **SEATS}).status_code == 400
    too_many = {**SEATS, "members": [SEATS["members"][0]] * 9}
    assert client.post(f"/api/council/sessions/{sid}/ask", json={"question": "q", **too_many}).status_code == 422


def test_delete_turn_and_session(client, db):
    sid = client.post("/api/council/sessions", json={}).json()["id"]
    client.post(f"/api/council/sessions/{sid}/ask", json={"question": "q1", **SEATS})
    tid = client.get(f"/api/council/sessions/{sid}").json()["turns"][0]["id"]
    assert client.delete(f"/api/council/sessions/{sid}/turns/{tid}").json() == {"deleted": True}
    assert client.get(f"/api/council/sessions/{sid}").json()["turns"] == []
    assert client.delete(f"/api/council/sessions/{sid}").json() == {"deleted": True}
    assert client.get(f"/api/council/sessions/{sid}").status_code == 404


def test_orphaned_running_turn_reads_as_interrupted(client, db):
    sid = client.post("/api/council/sessions", json={}).json()["id"]
    s = db()
    s.add(CouncilTurn(id="t-old", session_id=sid, owner="alice", question="q", status="running"))
    s.commit()
    s.close()
    assert client.get(f"/api/council/sessions/{sid}").json()["turns"][0]["status"] == "interrupted"
    assert client.post(f"/api/council/sessions/{sid}/turns/t-old/stop").json() == {"stopped": False}


def _handler(router, method, path):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(path)


def test_stop_cancels_a_running_council_and_keeps_finished_stages(db, monkeypatch):
    release = asyncio.Event()

    async def slow_chair(url, model, messages, **kwargs):
        if messages[0]["content"] == cr.council.CHAIR_SYSTEM:
            await release.wait()  # never set: the chairman hangs until stopped
        async for chunk in _fake_stream(url, model, messages, **kwargs):
            yield chunk

    monkeypatch.setattr("src.llm_core.stream_llm", slow_chair)
    router = cr.setup_council_routes()
    create = _handler(router, "POST", "/api/council/sessions")
    ask = _handler(router, "POST", "/api/council/sessions/{session_id}/ask")
    stop = _handler(router, "POST", "/api/council/sessions/{session_id}/turns/{turn_id}/stop")
    get = _handler(router, "GET", "/api/council/sessions/{session_id}")
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice", api_token=False),
                              app=SimpleNamespace(state=SimpleNamespace()), client=None)

    async def scenario():
        sid = create(request, cr.SessionCreate())["id"]
        resp = await ask(sid, request, cr.AskRequest(question="slow one", **SEATS))
        events = []
        turn_id = None
        async for raw in resp.body_iterator:
            event = json.loads(raw[5:])
            events.append(event)
            if event["type"] == "turn":
                turn_id = event["turn_id"]
            if event["type"] == "stage" and event["stage"] == "synthesis" and event["status"] == "start":
                assert await stop(sid, turn_id, request) == {"stopped": True}
            if event["type"] == "done":
                break
        return sid, turn_id, events

    sid, turn_id, events = asyncio.run(scenario())
    assert events[-1] == {"type": "done", "status": "cancelled", "turn_id": turn_id}
    (turn,) = get(sid, request)["turns"]
    assert turn["status"] == "cancelled"
    # Opinions and reviews finished before the stop, so they were kept.
    assert len(turn["opinions"]) == 2 and len(turn["reviews"]) == 2
    assert all(not o.get("error") for o in turn["opinions"])
    assert turn["final"] == ""
    assert not cr._RUNNING and not cr._ACTIVE_SESSIONS


def _direct(router, request):
    return (_handler(router, "POST", "/api/council/sessions"),
            _handler(router, "POST", "/api/council/sessions/{session_id}/ask"),
            _handler(router, "POST", "/api/council/sessions/{session_id}/turns/{turn_id}/stop"),
            _handler(router, "GET", "/api/council/sessions/{session_id}"))


def _request(user="alice"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user, api_token=False),
                           app=SimpleNamespace(state=SimpleNamespace()), client=None)


def test_second_ask_on_a_busy_council_is_refused(db, monkeypatch):
    from fastapi import HTTPException

    async def hang(url, model, messages, **kwargs):
        await asyncio.Event().wait()
        yield ""

    monkeypatch.setattr("src.llm_core.stream_llm", hang)
    create, ask, stop, _get = _direct(cr.setup_council_routes(), _request())
    request = _request()

    async def scenario():
        sid = create(request, cr.SessionCreate())["id"]
        first = await ask(sid, request, cr.AskRequest(question="one", **SEATS))
        with pytest.raises(HTTPException) as exc:
            await ask(sid, request, cr.AskRequest(question="two", **SEATS))
        assert exc.value.status_code == 409
        turn_id = None
        async for raw in first.body_iterator:
            turn_id = json.loads(raw[5:])["turn_id"]
            break
        assert await stop(sid, turn_id, request) == {"stopped": True}
        async for raw in first.body_iterator:
            if json.loads(raw[5:])["type"] == "done":
                break
        # Once stopped, the council takes questions again.
        assert sid not in cr._ACTIVE_SESSIONS

    asyncio.run(scenario())


def test_stop_during_synthesis_keeps_the_partial_answer(db, monkeypatch):
    async def chair_streams_then_hangs(url, model, messages, **kwargs):
        if messages[0]["content"] == cr.council.CHAIR_SYSTEM:
            yield 'data: {"delta": "The council leans towards"}\n\n'
            await asyncio.Event().wait()
        async for chunk in _fake_stream(url, model, messages, **kwargs):
            yield chunk

    monkeypatch.setattr("src.llm_core.stream_llm", chair_streams_then_hangs)
    create, ask, stop, get = _direct(cr.setup_council_routes(), _request())
    request = _request()

    async def scenario():
        sid = create(request, cr.SessionCreate())["id"]
        resp = await ask(sid, request, cr.AskRequest(question="q", **SEATS))
        turn_id = None
        async for raw in resp.body_iterator:
            ev = json.loads(raw[5:])
            turn_id = ev.get("turn_id", turn_id)
            if ev["type"] == "delta" and ev["stage"] == "synthesis":
                await stop(sid, turn_id, request)
            if ev["type"] == "done":
                break
        return sid

    sid = asyncio.run(scenario())
    (turn,) = get(sid, request)["turns"]
    assert turn["status"] == "cancelled"
    assert turn["final"].startswith("The council leans towards")
    assert "stopped before the chairman finished" in turn["final"]
