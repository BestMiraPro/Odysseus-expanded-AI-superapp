"""Materials service functions must resolve every name they reference.

The Transcribe button appears on materials whose PDF text layer is thin
(a scan or formula images). Clicking it 500'd on every material no matter the
model, because run_transcribe_material referenced TRANSCRIBE_PAGES_PER_CALL
and TRANSCRIBE_SYSTEM, which the routes/study split left unbound — the
NameError fires after page rendering but before any model call, so no model
choice could ever help. The same split left run_generate_notes and
run_generate_overview referencing a bare `request` that only route handlers
have, so Make notes and Overview 500'd the same way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.study._common as common
import routes.study.materials as materials
import src.study_service as service
import src.study_vision as vision


class _DB:
    def close(self):
        pass

    def commit(self):
        pass

    def query(self, *a, **k):
        return _EmptyQuery()


class _EmptyQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return []


@pytest.fixture
def fake_db(monkeypatch):
    monkeypatch.setattr(common, "SessionLocal", lambda: _DB())
    return _DB()


async def test_transcribe_batches_pages_and_stores_text(monkeypatch, fake_db):
    mat = SimpleNamespace(file_id="scan.pdf", content="", char_count=0,
                          page_count=21)
    monkeypatch.setattr(service, "get_material", lambda db, mid, user: mat)
    monkeypatch.setattr(common, "_resolve_uploaded_file", lambda fid: "/tmp/scan.pdf")
    monkeypatch.setattr(vision, "render_pdf_pages", lambda path: [b"p1", b"p2", b"p3", b"p4"])
    monkeypatch.setattr(vision, "pages_to_data_urls",
                        lambda pages: [f"url{i}" for i in range(len(pages))])

    calls = []

    async def _fake_vision(user, system, instruction, batch):
        calls.append(len(batch))
        assert system, "the transcribe system prompt must be a real prompt"
        return "[Page x text]:\n" + "transcribed content " * 10

    monkeypatch.setattr(common, "_llm_text_vision", _fake_vision)

    out = await materials.run_transcribe_material("alice", "mat-1")

    assert out["ok"] is True
    assert out["pages"] == 4
    assert out["pages_failed"] == 0
    # Four pages over TRANSCRIBE_PAGES_PER_CALL=3 go out as two calls.
    assert calls == [3, 1]
    assert mat.char_count == len(mat.content) > 100
    assert "[Page" in mat.content


async def test_generate_notes_gets_past_rate_limiting(monkeypatch, fake_db):
    """The limiter lives in the route handler now; the service function must
    not reference the handler's `request`."""
    mat = SimpleNamespace(content="short", name="n", file_id=None, kind="text")
    monkeypatch.setattr(service, "get_material", lambda db, mid, user: mat)

    with pytest.raises(HTTPException) as exc:
        await materials.run_generate_notes("alice", "mat-1")
    assert exc.value.status_code == 400  # not enough text, not a NameError


async def test_generate_overview_gets_past_rate_limiting(monkeypatch, fake_db):
    monkeypatch.setattr(service, "get_deck",
                        lambda db, did, user: SimpleNamespace(name="Econ"))
    fake_db.query = lambda *a, **k: _EmptyQuery()

    with pytest.raises(HTTPException) as exc:
        await materials.run_generate_overview("alice", "deck-1")
    assert exc.value.status_code == 400  # no materials, not a NameError
