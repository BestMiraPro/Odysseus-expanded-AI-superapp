"""Council work runs over HTTP: refusals, rounds saved as turns, stop, budget, persistence."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

import core.database as dbmod
import routes.council_routes as cr
import routes.council_work_routes as cwr
from core.database import Base, CouncilTurn, ModelEndpoint, SpendEntry
from src import budget, council, council_tools
from tests.test_council_workspace import _commit, _git, _snapshot

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PRICE = {"billing": "metered", "input_per_mtok": 2.0, "output_per_mtok": 8.0}      # $ per 1M tokens


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    for module in (dbmod, cr, cwr):
        monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr("src.model_context.get_context_length_known", lambda url, model: (128000, False))
    monkeypatch.setattr(cr, "_seat_pricing", lambda ep, model, kind, provider: dict(PRICE))
    budget.clear_price_cache()
    s = Session()
    s.add(ModelEndpoint(id="api1", name="Paid", base_url="https://api.example.com/v1", owner="alice",
                        is_enabled=True, model_type="llm", api_key="sk-1",
                        cached_models=json.dumps(["paid-a", "paid-b"])))
    s.commit()
    s.close()
    cr._RUNNING.clear()
    cr._ACTIVE_SESSIONS.clear()
    return Session


@pytest.fixture
def allowed(monkeypatch, tmp_path):
    """Everything a work run needs: Docker, login on for everyone, alice an admin."""
    monkeypatch.setattr(cwr, "in_docker", lambda: True)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: owner == "alice")
    monkeypatch.setattr(cwr, "runs_root", lambda: tmp_path / "runs")


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "owner-repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _commit(path, "README.md", "hello\n")
    _git(path, "branch", "feature")
    (path / "README.md").write_text("owner's unsaved edit\n")
    (path / "scratch.txt").write_text("untracked\n")
    return path


CHAIR = ("Merged both seats' work; tests pass.\n\n## Next round\n- seat-1: tokenizer\n- seat-2: cache\n\n"
         "## Verdict\n- Decision: keep going\n- Confidence: medium\n- Dissent: none\n"
         "- Would change it: a failing test\n- Next test: none\n- Status: {status}")


def _seat_git(cwd, env, *args):
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full.update({k: v for k, v in env.items() if v is not None})
    proc = subprocess.run(["git", *args], cwd=cwd, env=full, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


class Seats:
    """Stand-in for council_tools.stream_fn_for: seats that act on the run's real folder.

    Work: commit a file in the seat's worktree and write a message to all.
    Review: rank every response. Synthesis: merge the seat branches into main.
    ``hold`` makes round N's work hang until the run is stopped; ``usage``
    is reported by every call; ``during`` runs at the first call of a round.
    """

    def __init__(self, statuses=("continue",), hold=None, usage=None, during=None):
        self.statuses, self.hold, self.usage, self.during = statuses, hold, usage, during
        self.settings = []
        self.calls = []          # (round, stage, settings, headers)
        self.systems = []        # each call's system prompt

    def __call__(self, settings):
        self.settings.append(settings)

        async def stream(url, model, messages, *, stage="opinions", headers=None, **kwargs):
            round_no = int(re.search(r"round (\d+) of", messages[-1]["content"]).group(1))
            if self.during and not any(c[0] == round_no for c in self.calls):
                self.during(round_no)
            self.calls.append((round_no, stage, settings, headers))
            self.systems.append(messages[0]["content"])
            if stage == "opinions" and self.hold == round_no:
                await asyncio.Event().wait()
            text = await asyncio.to_thread(self._act, stage, round_no, settings, messages)
            yield _sse({"delta": text})
            if self.usage:
                yield _sse({"type": "usage", "data": self.usage})
            yield "data: [DONE]\n\n"

        stream.accepts_stage = True
        return stream

    def _act(self, stage, round_no, settings, messages):
        cwd, env = Path(settings.workspace), settings.env
        if stage == "opinions":
            who = env["GIT_AUTHOR_NAME"]
            (cwd / f"{who}-r{round_no}.txt").write_text(f"{who} round {round_no}\n")
            _seat_git(cwd, env, "add", "-A")
            _seat_git(cwd, env, "commit", "-q", "-m", f"{who} round {round_no}")
            (cwd.parents[1] / "messages" / "outbox" / who / "all__progress.md").write_text(
                f"{who} committed round {round_no}")
            return f"Changed: added {who}-r{round_no}.txt. Test: it exists. Result: it does."
        if stage == "review":
            labels = sorted(set(council._RESPONSE_LABEL_RE.findall(messages[-1]["content"])))
            return "Checked.\nFINAL RANKING:\n" + "\n".join(f"{i}. Response {l}" for i, l in enumerate(labels, 1))
        seats = sorted(p.name for p in (cwd.parent / "seats").iterdir())
        _seat_git(cwd, env, "merge", "-q", "--no-edit", *[f"seat-{n}" for n in seats])
        return CHAIR.format(status=self.statuses[min(round_no, len(self.statuses)) - 1])


@pytest.fixture
def client(db, allowed, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user", "alice")
        return await call_next(request)

    app.include_router(cr.setup_council_routes())
    app.include_router(cwr.setup_council_work_routes())
    return TestClient(app)


def _seats(monkeypatch, **kw):
    seats = Seats(**kw)
    monkeypatch.setattr(council_tools, "stream_fn_for", seats)
    return seats


def _events(text_):
    return [json.loads(line[5:]) for line in text_.splitlines() if line.startswith("data:")]


SEATS = {"members": [{"endpoint_id": "api1", "model": "paid-a"}, {"endpoint_id": "api1", "model": "paid-b"}],
         "chairman": {"endpoint_id": "api1", "model": "paid-a"}}
GOAL = "Make the parser 2x faster"


def _body(repo, **over):
    return {"goal": GOAL, **SEATS, "rounds": 3, "workspace": str(repo), **over}


def _start(client, sid, repo, **over):
    return client.post(f"/api/council/sessions/{sid}/work", json=_body(repo, **over))


def _session(client):
    return client.post("/api/council/sessions", json={}).json()["id"]


def _turns(client, sid):
    return client.get(f"/api/council/sessions/{sid}").json()["turns"]


def _no_turns(db):
    s = db()
    try:
        return s.query(CouncilTurn).count() == 0
    finally:
        s.close()


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

@needs_git
def test_a_non_admin_on_a_multi_user_install_is_refused(client, db, repo, monkeypatch):
    _seats(monkeypatch)
    sid = client.post("/api/council/sessions", json={}, headers={"x-user": "bob"}).json()["id"]
    resp = client.post(f"/api/council/sessions/{sid}/work", json=_body(repo), headers={"x-user": "bob"})
    assert resp.status_code == 403 and "admin" in resp.json()["detail"]
    assert _no_turns(db)


@needs_git
def test_a_folder_that_is_not_the_top_level_of_a_git_repository_is_refused(client, db, repo, tmp_path, monkeypatch):
    _seats(monkeypatch)
    sid = _session(client)
    plain = tmp_path / "plain"
    plain.mkdir()
    (repo / "src").mkdir()
    for folder in (plain, repo / "src"):
        resp = _start(client, sid, folder)
        assert resp.status_code == 400 and "top level of a git repository" in resp.json()["detail"]
    assert _start(client, sid, tmp_path / "missing").status_code == 400
    assert _no_turns(db) and not (tmp_path / "runs").exists()


@needs_git
@pytest.mark.parametrize("rounds", [0, 21])
def test_rounds_outside_1_to_20_are_refused(client, db, repo, monkeypatch, rounds):
    _seats(monkeypatch)
    assert _start(client, _session(client), repo, rounds=rounds).status_code == 422
    assert _no_turns(db)


@needs_git
def test_while_waiting_must_be_stop_continue_or_vote(client, db, repo, monkeypatch):
    _seats(monkeypatch)
    assert _start(client, _session(client), repo, while_waiting="ask").status_code == 422


@needs_git
def test_a_work_run_is_refused_outside_the_docker_container(client, db, repo, monkeypatch):
    _seats(monkeypatch)
    monkeypatch.setattr(cwr, "in_docker", lambda: False)
    resp = _start(client, _session(client), repo)
    assert resp.status_code == 403 and "Docker" in resp.json()["detail"]
    assert _no_turns(db)


@needs_git
@pytest.mark.parametrize("env,words", [({"AUTH_ENABLED": "false"}, "AUTH_ENABLED"),
                                       ({"LOCALHOST_BYPASS": "true"}, "LOCALHOST_BYPASS")])
def test_a_work_run_is_refused_when_an_unauthenticated_caller_could_get_in(client, db, repo, monkeypatch, env, words):
    _seats(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    resp = _start(client, _session(client), repo)
    assert resp.status_code == 403 and words in resp.json()["detail"] and "login" in resp.json()["detail"]
    assert _no_turns(db)


def test_docker_is_detected_from_the_marker_docker_creates(monkeypatch):
    seen = []
    monkeypatch.setattr("src.host_docker_access.running_in_container", lambda: seen.append(1) or True)
    assert cwr.in_docker() is True and seen


# ---------------------------------------------------------------------------
# a run
# ---------------------------------------------------------------------------

@needs_git
def test_n_rounds_are_saved_as_n_turns_with_work_data(client, db, repo, monkeypatch, tmp_path):
    seats = _seats(monkeypatch)
    before = _snapshot(repo)
    sid = _session(client)
    resp = _start(client, sid, repo, rounds=3, while_waiting="vote")
    assert resp.status_code == 200
    events = _events(resp.text)
    start = events[0]
    assert start["type"] == "work" and start["rounds"] == 3 and start["branch"].startswith("odysseus/make-the-parser")
    turn_events = [e for e in events if e["type"] == "turn"]
    assert [e["work"]["round"] for e in turn_events] == [1, 2, 3]
    assert all(e["question"].startswith(f"Work run, round {e['work']['round']} of 3.") for e in turn_events)
    assert all(e["session_id"] == sid and len(e["members"]) == 2 and "url" not in json.dumps(e) for e in turn_events)
    assert {e["round"] for e in events if e["type"] == "stage"} == {1, 2, 3}
    assert events[-1]["type"] == "done" and events[-1]["status"] == "done" and events[-1]["reason"] == "max_rounds"

    turns = _turns(client, sid)
    assert len(turns) == 3 and all(t["status"] == "done" for t in turns)
    run_id = turns[0]["work"]["run_id"]
    for n, turn in enumerate(turns, start=1):
        work = turn["work"]
        assert work["run_id"] == run_id == start["run_id"] and work["round"] == n and work["rounds"] == 3
        assert work["goal"] == GOAL and work["source"] == str(repo.resolve())
        assert work["branch"] == start["branch"] and work["while_waiting"] == "vote"
        assert work["run_status"] == "finished" and work["stop_reason"] == "max_rounds"
        assert re.fullmatch(r"[0-9a-f]{40}", work["base_sha"]) and work["head_sha"] != work["base_sha"]
        assert turn["question"].startswith(f"Work run, round {n} of 3.")
        progress = [m for m in work["messages"] if m["name"].endswith("__all__progress.md")]
        assert sorted(m["from"] for m in progress) == ["seat-1", "seat-2"]
        assert all(m["to"] == "all" and m["text"] == f"{m['from']} committed round {n}" for m in progress)
        # Each seat's diff rides next to its report, never inside it.
        for op in turn["opinions"]:
            assert f"seat-{op['member'] + 1}-r{n}.txt" in op["attachment"]
            assert "diff --git" not in op["text"]
        assert turn["config"]["tools"] == "full"
    assert turns[1]["work"]["base_sha"] == turns[0]["work"]["head_sha"]

    # Every seat ran with full tools (research to review) inside the run's own folder.
    run_root = tmp_path / "runs" / run_id
    assert {s.mode for s in seats.settings} == {"full", "research"}
    assert all(s.owner == "alice" and Path(s.workspace).is_relative_to(run_root) for s in seats.settings)
    assert all(s.env.get("GIT_AUTHOR_NAME") for s in seats.settings)
    # The owner's folder was only read, and the run's folder is kept.
    assert _snapshot(repo) == before
    assert (run_root / "main" / "seat-1-r3.txt").exists()
    assert sid not in cr._ACTIVE_SESSIONS and not cr._RUNNING


@needs_git
def test_the_run_stops_early_when_the_chairman_says_done(client, db, repo, monkeypatch):
    _seats(monkeypatch, statuses=("continue", "done"))
    sid = _session(client)
    events = _events(_start(client, sid, repo, rounds=5).text)
    assert events[-1]["reason"] == "done"
    turns = _turns(client, sid)
    assert [t["work"]["round"] for t in turns] == [1, 2]
    assert turns[-1]["verdict"]["status"] == "done"
    assert all(t["work"]["stop_reason"] == "done" for t in turns)


@needs_git
def test_the_session_remembers_rounds_and_while_waiting(client, db, repo, monkeypatch):
    _seats(monkeypatch, statuses=("done",))
    sid = _session(client)
    _start(client, sid, repo, rounds=7, while_waiting="continue")
    config = client.get(f"/api/council/sessions/{sid}").json()["config"]
    assert config["rounds"] == 7 and config["while_waiting"] == "continue"
    assert config["tools"] == "full" and config["workspace"] == str(repo.resolve())
    assert config["members"] == SEATS["members"]
    # A new council starts from the same choices.
    assert client.post("/api/council/sessions", json={}).json()["config"]["rounds"] == 7


@needs_git
def test_the_seats_are_resolved_again_each_round_so_tokens_refresh(client, db, repo, monkeypatch):
    seats = _seats(monkeypatch, statuses=("continue", "done"))
    real = cr.resolve_seat
    issued = []

    def fresh(db_, owner, seat, runtime=True):
        member = real(db_, owner, seat, runtime)
        issued.append(1)
        member.headers = {"Authorization": f"Bearer token-{len(issued)}"}
        return member

    monkeypatch.setattr(cr, "resolve_seat", fresh)
    _start(client, _session(client), repo, rounds=2)
    tokens = {}
    for round_no, _stage, _settings, headers in seats.calls:
        tokens.setdefault(round_no, set()).add(headers["Authorization"])
    assert len(issued) == 3 * 3                 # 2 seats + chairman: at start, then once per round
    assert not tokens[1] & tokens[2]            # round 2 ran on newly resolved seats


@needs_git
def test_a_run_that_fails_midway_keeps_its_rounds_says_why_and_frees_the_council(client, db, repo, monkeypatch):
    _seats(monkeypatch)
    real = cr.resolve_seat
    calls = []

    def flaky(db_, owner, seat, runtime=True):
        calls.append(1)
        if len(calls) > 6:                          # start and round 1 resolve; round 2 cannot
            raise council.CouncilError("Paid needs reconnecting before it can sit on the council.", 401)
        return real(db_, owner, seat, runtime)

    monkeypatch.setattr(cr, "resolve_seat", flaky)
    sid = _session(client)
    done = _events(_start(client, sid, repo, rounds=3).text)[-1]
    assert done["status"] == "error" and done["reason"] == "error" and "reconnecting" in done["error"]
    (turn,) = _turns(client, sid)
    assert turn["status"] == "done" and turn["work"]["stop_reason"] == "error"
    assert turn["work"]["run_status"] == "finished"
    assert sid not in cr._ACTIVE_SESSIONS and not cr._RUNNING


@needs_git
@pytest.mark.parametrize("memory", [True, False])
def test_memory_reaches_every_seat_only_when_turned_on(client, db, repo, monkeypatch, memory):
    seats = _seats(monkeypatch, statuses=("done",))
    monkeypatch.setattr(cr, "_memory_context", lambda owner, question, history, wait=True: "ABOUT THE OWNER: terse")
    sid = _session(client)
    _start(client, sid, repo, memory=memory)
    assert len(seats.systems) == 5
    assert all(("ABOUT THE OWNER: terse" in s) is memory for s in seats.systems)
    assert _turns(client, sid)[0]["config"]["memory"] is memory


@needs_git
def test_export_of_a_round_carries_its_work_data(client, db, repo, monkeypatch):
    _seats(monkeypatch, statuses=("continue", "done"))
    sid = _session(client)
    _start(client, sid, repo, rounds=4)
    turn = _turns(client, sid)[0]
    md = client.get(f"/api/council/sessions/{sid}/turns/{turn['id']}/export.md").text
    assert "- Work run: round 1 of 4" in md and f"- Branch: {turn['work']['branch']}" in md
    assert "- Stop reason: done" in md
    assert "### seat-1 → all" in md and "seat-1 committed round 1" in md
    assert "Diff attached by Odysseus" in md and "seat-1-r1.txt" in md


# ---------------------------------------------------------------------------
# spend and budget
# ---------------------------------------------------------------------------

USAGE = {"input_tokens": 1000, "output_tokens": 100}
CALL_USD = (1000 * 2.0 + 100 * 8.0) / 1e6           # one call at PRICE
ROUND_CALLS = 5                                      # 2 opinions, 2 reviews, 1 chairman


def _spend_rows(db):
    s = db()
    try:
        return s.query(SpendEntry).all()
    finally:
        s.close()


@needs_git
def test_spend_is_recorded_after_every_round(client, db, repo, monkeypatch):
    seen = {}
    _seats(monkeypatch, statuses=("continue", "done"), usage=USAGE,
           during=lambda round_no: seen.setdefault(round_no, len(_spend_rows(db))))
    sid = _session(client)
    _start(client, sid, repo, rounds=3)
    rows = _spend_rows(db)
    assert seen == {1: 0, 2: ROUND_CALLS}               # round 1 was billed before round 2 started
    assert len(rows) == 2 * ROUND_CALLS and {r.source for r in rows} == {"council"}
    assert {r.session_id for r in rows} == {sid}
    assert budget.month_spend("alice") == pytest.approx(2 * ROUND_CALLS * CALL_USD)


@needs_git
def test_the_whole_run_goes_through_the_budget_check_once_with_the_confirm_round_trip(client, db, repo, monkeypatch):
    _seats(monkeypatch, statuses=("done",), usage=USAGE)
    budget.save_settings("alice", {"action_limit_usd": 0.0001})
    sid = _session(client)
    first = _start(client, sid, repo, rounds=4)
    assert first.status_code == 402
    detail = first.json()["detail"]
    assert detail["code"] == "budget_confirm" and "work run" in detail["message"]
    est = detail["estimate"]
    assert est["rounds"] == 4 and est["total_usd"] == pytest.approx(est["per_round_usd"] * 4)
    assert _no_turns(db) and sid not in cr._ACTIVE_SESSIONS
    ok = _start(client, sid, repo, rounds=4, budget_confirmed=True)
    assert ok.status_code == 200 and _events(ok.text)[-1]["reason"] == "done"


@needs_git
def test_the_confirmed_total_is_the_ceiling_that_stops_the_run(client, db, repo, monkeypatch):
    # Each call costs far more than estimated: after round 1 the next round would pass the ceiling.
    _seats(monkeypatch, usage={"input_tokens": 400_000, "output_tokens": 50_000})
    sid = _session(client)
    events = _events(_start(client, sid, repo, rounds=5, budget_confirmed=True).text)
    assert events[-1]["reason"] == "budget"
    assert [t["work"]["round"] for t in _turns(client, sid)] == [1]


@needs_git
def test_each_round_rechecks_the_monthly_cap(client, db, repo, monkeypatch):
    budget.save_settings("alice", {"monthly_cap_usd": 1.0, "cap_action": "block", "action_limit_usd": 0})

    def owner_spends_elsewhere(round_no):
        if round_no == 1:
            s = db()
            s.add(SpendEntry(owner="alice", model="paid-x", source="chat", input_tokens=1, output_tokens=1,
                             cost_usd=1.0, created_at=budget._utcnow()))
            s.commit()
            s.close()

    _seats(monkeypatch, usage=USAGE, during=owner_spends_elsewhere)
    sid = _session(client)
    events = _events(_start(client, sid, repo, rounds=5).text)
    assert events[-1]["reason"] == "budget"
    assert len(_turns(client, sid)) == 1


# ---------------------------------------------------------------------------
# stop and busy (driven on one event loop, as the server runs them)
# ---------------------------------------------------------------------------

def _handler(router, method, path):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(path)


def _request(user="alice"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user, api_token=False),
                           app=SimpleNamespace(state=SimpleNamespace()), client=None)


def _handlers():
    council_router, work_router = cr.setup_council_routes(), cwr.setup_council_work_routes()
    return (_handler(council_router, "POST", "/api/council/sessions"),
            _handler(work_router, "POST", "/api/council/sessions/{session_id}/work"),
            _handler(council_router, "POST", "/api/council/sessions/{session_id}/turns/{turn_id}/stop"),
            _handler(council_router, "GET", "/api/council/sessions/{session_id}"),
            _handler(council_router, "POST", "/api/council/sessions/{session_id}/ask"))


@needs_git
def test_stop_ends_the_whole_run_and_keeps_the_finished_rounds(db, allowed, repo, monkeypatch):
    _seats(monkeypatch, hold=2)
    create, work, stop, get, _ask = _handlers()
    request = _request()

    async def scenario():
        sid = create(request, cr.SessionCreate())["id"]
        resp = await work(sid, request, cwr.WorkRequest(**_body(repo, rounds=5)))
        events = []
        async for raw in resp.body_iterator:
            event = json.loads(raw[5:])
            events.append(event)
            if event["type"] == "turn" and event["work"]["round"] == 2:
                assert await stop(sid, event["turn_id"], request) == {"stopped": True}
            if event["type"] == "done":
                break
        return sid, events

    sid, events = asyncio.run(scenario())
    assert events[-1]["status"] == "cancelled" and events[-1]["reason"] == "cancelled"
    turns = get(sid, request)["turns"]
    assert [(t["work"]["round"], t["status"]) for t in turns] == [(1, "done"), (2, "cancelled")]
    assert turns[0]["final"] and len(turns[0]["opinions"]) == 2
    assert all(t["work"]["run_status"] == "finished" and t["work"]["stop_reason"] == "cancelled" for t in turns)
    assert not cr._RUNNING and sid not in cr._ACTIVE_SESSIONS


@needs_git
def test_a_second_start_while_running_is_refused(db, allowed, repo, monkeypatch):
    _seats(monkeypatch, hold=1)
    create, work, stop, _get, ask = _handlers()
    request = _request()

    async def scenario():
        sid = create(request, cr.SessionCreate())["id"]
        first = await work(sid, request, cwr.WorkRequest(**_body(repo)))
        with pytest.raises(HTTPException) as again:
            await work(sid, request, cwr.WorkRequest(**_body(repo)))
        assert again.value.status_code == 409
        # An ordinary question on the same council waits too.
        with pytest.raises(HTTPException) as plain:
            await ask(sid, request, cr.AskRequest(question="and?", **SEATS))
        assert plain.value.status_code == 409
        async for raw in first.body_iterator:
            event = json.loads(raw[5:])
            if event["type"] == "turn":
                await stop(sid, event["turn_id"], request)
            if event["type"] == "done":
                break
        assert sid not in cr._ACTIVE_SESSIONS

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def _old_council_turns(engine):
    """council_turns as it was before work runs: no work column."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE council_turns (id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL, owner VARCHAR, "
            "question TEXT NOT NULL, config TEXT, status VARCHAR NOT NULL, opinions TEXT, reviews TEXT, "
            "ranking TEXT, labels TEXT, final TEXT, error TEXT, usage TEXT, chair TEXT, "
            "created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("INSERT INTO council_turns (id, session_id, question, status) VALUES ('t1', 's1', 'q', 'done')"))


def _columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("council_turns")}


def test_the_work_column_is_added_to_a_database_created_without_it(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", poolclass=NullPool)
    _old_council_turns(engine)
    assert "work" not in _columns(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    dbmod._migrate_add_council_work_column()
    assert "work" in _columns(engine)
    dbmod._migrate_add_council_work_column()          # a second start changes nothing
    with engine.connect() as conn:
        assert conn.execute(text("SELECT work FROM council_turns WHERE id = 't1'")).scalar() is None


def test_schema_reconciliation_also_adds_the_work_column(tmp_path):
    # The dialect-agnostic path (PostgreSQL has no PRAGMA table_info).
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", poolclass=NullPool)
    _old_council_turns(engine)
    assert "council_turns.work" in dbmod.reconcile_schema_with_models(engine)
    assert "work" in _columns(engine)
