"""Vision model selection and PDF work for Study's image pipeline.

- The Study model was always the first vision candidate, even when text-only:
  every page batch of a vision extraction (dozens of calls) failed on it
  before falling back, and with no real vision model configured vision still
  looked available. Only vision-capable models are candidates now, with a
  dedicated (configured) vision model first.
- Rendering PDF pages and pulling figures ran synchronously inside async
  handlers, stalling the event loop for every other request for seconds.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.study._common as common

REMOTE = "https://api.example.com/v1"   # not LM Studio: capability is name-based


def _setup(monkeypatch, *, study_model, configured="", auto=None, fallbacks=()):
    import src.document_processor as dp
    import src.endpoint_resolver as er

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_model": configured})

    def resolve_vl(name, owner=None):
        if name:
            return (REMOTE, name, {})
        if auto:
            return (REMOTE, auto, {})
        raise ValueError("No vision model available")

    monkeypatch.setattr(dp, "_resolve_vl_model", resolve_vl)
    monkeypatch.setattr(er, "resolve_endpoint",
                        lambda prefix, owner=None, **kw: (REMOTE, study_model, {}))
    monkeypatch.setattr(er, "resolve_vision_fallback_candidates",
                        lambda owner=None: [(REMOTE, m, {}) for m in fallbacks])


def _models(owner="alice"):
    return [m for _url, m, _h in common._vision_candidates(owner)]


def test_text_only_study_model_is_not_a_vision_candidate(monkeypatch):
    _setup(monkeypatch, study_model="llama-3.1-8b-instruct")
    assert _models() == []


def test_dedicated_vision_model_comes_first(monkeypatch):
    _setup(monkeypatch, study_model="qwen2-vl-7b", configured="gpt-4o",
           fallbacks=("llava-13b",))
    assert _models() == ["gpt-4o", "qwen2-vl-7b", "llava-13b"]


def test_text_only_study_model_is_skipped_but_vision_models_stay(monkeypatch):
    _setup(monkeypatch, study_model="mistral-7b-instruct", configured="gpt-4o")
    assert _models() == ["gpt-4o"]


def test_a_vision_study_model_leads_the_auto_detected_one(monkeypatch):
    _setup(monkeypatch, study_model="qwen2-vl-7b", auto="gpt-4o-mini")
    assert _models() == ["qwen2-vl-7b", "gpt-4o-mini"]


def test_no_vision_model_fails_fast_without_calling_a_model(monkeypatch):
    """A text-only setup gets the actionable 503 at once, not a model call
    per page batch that cannot succeed."""
    import src.llm_core as llm_core
    _setup(monkeypatch, study_model="llama-3.1-8b-instruct")

    async def no_call(**kw):
        raise AssertionError("a text-only model was sent images")

    monkeypatch.setattr(llm_core, "llm_call_async", no_call)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(common._llm_json_vision("alice", "sys", "go", ["data:image/png;base64,"]))
    assert exc.value.status_code == 503


# ---------------------------------------------------------------- off the loop

class _Recorder:
    """A stand-in for a blocking PDF call that notes which thread ran it."""

    def __init__(self, result=None, exc=None):
        self.threads = []
        self.result, self.exc = result, exc

    def __call__(self, *a, **k):
        self.threads.append(threading.get_ident())
        if self.exc:
            raise self.exc
        return self.result


async def _loop_thread():
    return threading.get_ident()


def test_vision_extraction_renders_pages_off_the_event_loop(monkeypatch):
    import src.study_vision as sv
    render = _Recorder(exc=RuntimeError("stop after rendering"))
    monkeypatch.setattr(sv, "render_pdf_pages", render)

    async def go():
        loop_thread = await _loop_thread()
        with pytest.raises(HTTPException):
            await common._extract_questions_vision("alice", "extract", ["open"], "/x.pdf")
        return loop_thread

    loop_thread = asyncio.run(go())
    assert render.threads and render.threads[0] != loop_thread


def test_transcribe_renders_pages_off_the_event_loop(monkeypatch):
    import routes.study.materials as materials
    import src.study_service as service
    import src.study_vision as sv

    class _DB:
        def close(self):
            pass

    monkeypatch.setattr(common, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(service, "get_material",
                        lambda db, mid, user: SimpleNamespace(file_id="scan.pdf"))
    monkeypatch.setattr(common, "_resolve_uploaded_file", lambda fid: "/tmp/scan.pdf")
    render = _Recorder(exc=RuntimeError("stop after rendering"))
    monkeypatch.setattr(sv, "render_pdf_pages", render)

    async def go():
        loop_thread = await _loop_thread()
        with pytest.raises(HTTPException):
            await materials.run_transcribe_material("alice", "m1")
        return loop_thread

    loop_thread = asyncio.run(go())
    assert render.threads and render.threads[0] != loop_thread


def test_figure_extraction_runs_off_the_event_loop(monkeypatch, tmp_path):
    import src.study_vision as sv
    extract = _Recorder(result=[])
    monkeypatch.setattr(sv, "extract_pdf_figures", extract)
    monkeypatch.setattr(common, "_study_figures_dir", lambda mid: str(tmp_path))

    async def go():
        loop_thread = await _loop_thread()
        out = await common._build_figures_section("alice", "m1", "f.pdf", "/x.pdf")
        return loop_thread, out

    loop_thread, out = asyncio.run(go())
    assert out == ""
    assert extract.threads and extract.threads[0] != loop_thread
