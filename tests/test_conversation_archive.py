"""Conversation archive: parsing each export format, import, search, read."""
import io
import json
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as dbmod
from core.database import Base
from src import conversation_archive as ca

CLAUDE_AI = [
    {
        "uuid": "c-1",
        "name": "Portalegre farm budget",
        "created_at": "2026-03-01T10:00:00.000000Z",
        "updated_at": "2026-03-01T10:05:00.000000Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "created_at": "2026-03-01T10:00:00Z",
             "text": "How much does an olive grove in Alto Alentejo cost per hectare?",
             "content": [{"type": "text", "text": "How much does an olive grove in Alto Alentejo cost per hectare?"}]},
            {"uuid": "m2", "sender": "assistant", "created_at": "2026-03-01T10:01:00Z",
             "text": "", "content": [{"type": "text", "text": "Roughly 8,000 to 15,000 EUR per hectare."},
                                     {"type": "tool_use", "name": "web_search", "input": {}}]},
        ],
    },
    {"uuid": "c-empty", "name": "", "chat_messages": []},
]

CHATGPT = [
    {
        "title": "FSRS scheduling",
        "conversation_id": "g-1",
        "create_time": 1767225600.0,
        "update_time": 1767225900.0,
        "current_node": "n4",
        "mapping": {
            "n0": {"id": "n0", "message": None, "parent": None, "children": ["n1"]},
            "n1": {"id": "n1", "parent": "n0", "children": ["n2", "n2b"],
                   "message": {"author": {"role": "user"}, "create_time": 1767225600.0,
                               "content": {"content_type": "text", "parts": ["Explain FSRS stability"]}}},
            "n2b": {"id": "n2b", "parent": "n1", "children": [],
                    "message": {"author": {"role": "assistant"},
                                "content": {"content_type": "text", "parts": ["ABANDONED BRANCH"]}}},
            "n2": {"id": "n2", "parent": "n1", "children": ["n3"],
                   "message": {"author": {"role": "assistant"}, "create_time": 1767225610.0,
                               "content": {"content_type": "text", "parts": ["Stability is the interval at 90% recall."]}}},
            "n3": {"id": "n3", "parent": "n2", "children": ["n4"],
                   "message": {"author": {"role": "tool"},
                               "content": {"content_type": "text", "parts": ["tool output"]}}},
            "n4": {"id": "n4", "parent": "n3", "children": [],
                   "message": {"author": {"role": "assistant"}, "create_time": 1767225620.0,
                               "content": {"content_type": "code", "text": "print('desired retention')"}}},
        },
    }
]

CLAUDE_CODE_LINES = [
    {"type": "summary", "summary": "Fix Study optimizer", "leafUuid": "x"},
    {"type": "user", "sessionId": "cc-1", "cwd": "/home/dinis/odysseus", "timestamp": "2026-09-01T09:00:00Z",
     "message": {"role": "user", "content": "The FSRS optimizer never trains on lapses"}},
    {"type": "user", "sessionId": "cc-1", "isMeta": True, "message": {"role": "user", "content": "<command-name>/clear"}},
    {"type": "assistant", "sessionId": "cc-1", "timestamp": "2026-09-01T09:00:05Z",
     "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"},
                                                  {"type": "text", "text": "Found it: failures are filtered out."},
                                                  {"type": "tool_use", "name": "Edit", "input": {}}]}},
    {"type": "user", "sessionId": "cc-1",
     "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
]

CODEX_LINES = [
    {"timestamp": "2026-09-02T08:00:00Z", "type": "session_meta",
     "payload": {"id": "cx-1", "timestamp": "2026-09-02T08:00:00Z", "cwd": "/home/dinis/quant"}},
    {"timestamp": "2026-09-02T08:00:01Z", "type": "response_item",
     "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
    {"timestamp": "2026-09-02T08:00:02Z", "type": "response_item",
     "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "Backtest the TSMOM sleeve on BTC"}]}},
    {"timestamp": "2026-09-02T08:00:09Z", "type": "response_item",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Sharpe 1.1 after costs."}]}},
    {"timestamp": "2026-09-02T08:00:10Z", "type": "event_msg", "payload": {"type": "token_count"}},
]

CODEX_OLD_LINES = [
    {"id": "cx-old", "timestamp": "2025-05-01T08:00:00Z", "instructions": None},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old format question"}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Old format answer"}]},
]


def _jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    return Session


# ── Parsing ──

def test_parse_claude_ai_keeps_text_blocks_and_skips_empty():
    convs = list(ca.parse_conversations_json(CLAUDE_AI))
    assert len(convs) == 1
    c = convs[0]
    assert (c.source, c.external_id, c.title) == ("claude_ai", "c-1", "Portalegre farm budget")
    assert [m[0] for m in c.messages] == ["user", "assistant"]
    assert c.messages[1][1] == "Roughly 8,000 to 15,000 EUR per hectare."


def test_parse_chatgpt_follows_current_branch_only():
    (c,) = ca.parse_conversations_json(CHATGPT)
    texts = [m[1] for m in c.messages]
    assert texts == ["Explain FSRS stability", "Stability is the interval at 90% recall.",
                     "print('desired retention')"]
    assert "ABANDONED BRANCH" not in " ".join(texts)
    assert c.started_at.year == 2026


def test_parse_claude_code_session():
    (c,) = ca._parse_jsonl_text(_jsonl(CLAUDE_CODE_LINES), "proj/cc-1.jsonl")
    assert (c.source, c.external_id, c.title, c.project) == (
        "claude_code", "cc-1", "Fix Study optimizer", "/home/dinis/odysseus")
    assert [m[1] for m in c.messages] == ["The FSRS optimizer never trains on lapses",
                                          "Found it: failures are filtered out."]


def test_parse_codex_rollout_new_and_old_layouts():
    (c,) = ca._parse_jsonl_text(_jsonl(CODEX_LINES), "rollout-2026-09-02T08-00-00-cx-1.jsonl")
    assert (c.source, c.external_id, c.project) == ("codex", "cx-1", "/home/dinis/quant")
    assert [m[1] for m in c.messages] == ["Backtest the TSMOM sleeve on BTC", "Sharpe 1.1 after costs."]
    (old,) = ca._parse_jsonl_text(_jsonl(CODEX_OLD_LINES), "rollout-old.jsonl")
    assert old.external_id == "cx-old"
    assert [m[0] for m in old.messages] == ["user", "assistant"]


def _mixed_zip() -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("claude-export/conversations.json", json.dumps(CLAUDE_AI))
        zf.writestr("chatgpt/conversations.json", json.dumps(CHATGPT))
        zf.writestr("projects/-home-dinis-odysseus/cc-1.jsonl", _jsonl(CLAUDE_CODE_LINES))
        zf.writestr("sessions/2026/09/02/rollout-cx-1.jsonl", _jsonl(CODEX_LINES))
        zf.writestr("__MACOSX/._conversations.json", "junk")
        zf.writestr("chatgpt/chat.html", "<html></html>")
    buf.seek(0)
    return buf


# ── Import, search, read ──

def test_import_zip_search_read_and_reimport(db):
    counts = ca.import_fileobj("dinis", _mixed_zip(), "export.zip")
    assert counts == {"claude_ai": 1, "chatgpt": 1, "claude_code": 1, "codex": 1}

    hits = ca.search("dinis", "olive grove hectare")
    assert hits and hits[0]["source"] == "claude_ai"
    assert "«" in hits[0]["snippet"]

    only_codex = ca.search("dinis", "Sharpe", sources=["codex"])
    assert [h["source"] for h in only_codex] == ["codex"]
    assert ca.search("dinis", "Sharpe", sources=["chatgpt"]) == []

    conv = ca.read("dinis", hits[0]["conversation_id"], start=1, limit=5)
    assert conv["message_count"] == 2
    assert [m["position"] for m in conv["messages"]] == [1]
    assert "more messages" not in ca.format_conversation(conv)

    # Importing the same export again replaces instead of duplicating.
    ca.import_fileobj("dinis", _mixed_zip(), "export.zip")
    assert ca.stats("dinis")["total"] == 4
    assert len(ca.search("dinis", "olive grove")) == 1


def test_archive_is_owner_scoped(db):
    ca.import_fileobj("dinis", _mixed_zip(), "export.zip")
    assert ca.search("someone-else", "olive") == []
    hit = ca.search("dinis", "olive")[0]
    assert ca.read("someone-else", hit["conversation_id"]) is None
    assert ca.stats("someone-else")["total"] == 0


def test_search_query_cannot_inject_fts_syntax(db):
    ca.import_fileobj("dinis", _mixed_zip(), "export.zip")
    # Quotes, column filters and operators are plain words, never FTS syntax.
    assert ca._fts_query('olive" OR content:*') == '"olive" "OR" "content"'
    assert isinstance(ca.search("dinis", 'olive" OR content:*'), list)
    assert ca.search("dinis", "NEAR(") == []


def test_delete_source(db):
    ca.import_fileobj("dinis", _mixed_zip(), "export.zip")
    assert ca.delete_source("dinis", "chatgpt") == 1
    assert "chatgpt" not in ca.stats("dinis")["sources"]
    assert ca.search("dinis", "FSRS stability") == []


def test_scan_local_dirs(db, tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-home-dinis"
    proj.mkdir(parents=True)
    (proj / "cc-1.jsonl").write_text(_jsonl(CLAUDE_CODE_LINES))
    result = ca.scan_local("dinis", [str(tmp_path / ".claude" / "projects"), str(tmp_path / "missing")])
    assert result["imported"] == {"claude_code": 1}
    assert result["scanned"] == [str(tmp_path / ".claude" / "projects")]


def test_zip_member_size_guard(db, monkeypatch):
    monkeypatch.setattr(ca, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(ValueError):
        ca.import_fileobj("dinis", _mixed_zip(), "export.zip")


# ── API ──

def _client():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes.archive_routes import setup_archive_routes

    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = "dinis"
        return await call_next(request)

    app.include_router(setup_archive_routes())
    return TestClient(app)


def test_archive_api_import_search_read_delete(db):
    client = _client()
    r = client.post("/api/archive/import", files={"file": ("export.zip", _mixed_zip().getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    assert r.json()["imported"]["chatgpt"] == 1

    results = client.get("/api/archive/search", params={"q": "TSMOM"}).json()["results"]
    assert results[0]["source"] == "codex"
    conv = client.get(f"/api/archive/conversations/{results[0]['conversation_id']}").json()
    assert conv["messages"][0]["content"] == "Backtest the TSMOM sleeve on BTC"

    stats = client.get("/api/archive/stats").json()
    assert stats["sources"]["claude_ai"]["conversations"] == 1
    assert client.delete("/api/archive/sources/codex").json()["removed"] == 1


def test_archive_api_rejects_files_without_conversations(db):
    r = _client().post("/api/archive/import", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400


# ── Agent tools ──

def test_agent_tools_search_and_read_history(db):
    import asyncio

    from src.agent_tools import ToolBlock  # import first to avoid a circular import
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_schemas import function_call_to_tool_block

    def run(block, owner="dinis"):
        _desc, result = asyncio.run(execute_tool_block(block, owner=owner,
                                                       security_context=NO_TOOL_SECURITY_CONTEXT))
        return result

    ca.import_fileobj("dinis", _mixed_zip(), "export.zip")
    block = function_call_to_tool_block("search_history", json.dumps({"query": "TSMOM", "source": "codex"}))
    assert block.tool_type == "search_history"
    found = run(block)["results"]
    assert "Codex" in found and "TSMOM" in found
    conv_id = found.split("id=")[1].split()[0]
    read = run(function_call_to_tool_block("read_history", json.dumps({"conversation_id": conv_id})))["results"]
    assert "Sharpe 1.1 after costs." in read
    assert "No matching" in run(ToolBlock("search_history", "nothing like this"))["results"]
    assert "archive is empty" in run(ToolBlock("search_history", "olive"), owner="nobody")["results"]
