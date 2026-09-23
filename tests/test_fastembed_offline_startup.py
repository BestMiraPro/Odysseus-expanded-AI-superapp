"""FastEmbed startup behaviour (slow-restart fix).

Two contracts pinned here, because a drift in either reintroduces the
multi-minute startup stall:

1. Construction is offline-first: a fully cached model must be built with
   ``local_files_only=True`` so fastembed never calls huggingface.co
   (``model_info``/``list_repo_tree``) for a model it already has. Only a
   genuinely incomplete cache may fall through to the networked path.
2. The client is cached per model per process: RAG, memory and the tool
   index each build embedding lanes, and each used to construct a fresh
   ONNX model of the same file.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import src.embeddings as embeddings


class FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding; records construction kwargs."""

    constructions: list = []
    fail_local = False

    def __init__(self, model_name=None, cache_dir=None, **kwargs):
        FakeTextEmbedding.constructions.append(kwargs)
        if kwargs.get("local_files_only") and FakeTextEmbedding.fail_local:
            raise RuntimeError("cache incomplete")

    def embed(self, texts):
        for t in texts:
            yield [0.0, 1.0]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    FakeTextEmbedding.constructions = []
    FakeTextEmbedding.fail_local = False
    monkeypatch.setitem(sys.modules, "fastembed",
                        types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    monkeypatch.setattr(embeddings, "_fastembed_clients", {})
    yield


def test_construction_is_offline_first():
    embeddings.FastEmbedClient()

    assert FakeTextEmbedding.constructions == [{"local_files_only": True}], (
        "a cached model was constructed without local_files_only — fastembed "
        "will make huggingface.co calls and can stall for minutes"
    )


def test_incomplete_cache_falls_back_to_the_download_path():
    FakeTextEmbedding.fail_local = True

    embeddings.FastEmbedClient()

    assert FakeTextEmbedding.constructions == [
        {"local_files_only": True},
        {},
    ], "the networked download must remain available for a missing model"


def test_client_is_built_once_per_model():
    first = embeddings.get_fastembed_client("sentence-transformers/all-MiniLM-L6-v2")
    second = embeddings.get_fastembed_client("sentence-transformers/all-MiniLM-L6-v2")

    assert first is second, "the lanes must share one client (and one ONNX session)"
    assert len(FakeTextEmbedding.constructions) == 1

    other = embeddings.get_fastembed_client("BAAI/bge-small-en-v1.5")
    assert other is not first
    assert len(FakeTextEmbedding.constructions) == 2


# ---------------------------------------------------------------------------
# Cache metadata written on the other OS
# ---------------------------------------------------------------------------

WINDOWS_KEYS = {
    r"snapshots\5f1b8cd7\model.onnx": {"size": 90387630, "blob_id": "bbd7b466"},
    r"snapshots\5f1b8cd7\config.json": {"size": 650, "blob_id": "56c8c186"},
}


def _metadata_file(tmp_path, payload):
    model_dir = tmp_path / "models--qdrant--all-MiniLM-L6-v2-onnx"
    model_dir.mkdir()
    path = model_dir / "files_metadata.json"
    path.write_text(payload, encoding="utf-8")
    return path


def test_metadata_written_on_windows_is_normalized(tmp_path):
    """fastembed keys each cached file by its path relative to the model dir,
    with the separator of the OS that wrote it. A cache written by a Windows
    run keys "snapshots\\<rev>\\model.onnx", which the Linux container cannot
    find, so verification fails and every start logs "Local file sizes do not
    match the metadata" as though the model were being re-downloaded."""
    path = _metadata_file(tmp_path, json.dumps(WINDOWS_KEYS))

    embeddings._normalize_cache_metadata(str(tmp_path))

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "snapshots/5f1b8cd7/model.onnx": {"size": 90387630, "blob_id": "bbd7b466"},
        "snapshots/5f1b8cd7/config.json": {"size": 650, "blob_id": "56c8c186"},
    }, "sizes and blob ids must survive; only the separator changes"


def test_normalizing_rewrites_nothing_when_already_portable(tmp_path):
    payload = json.dumps({"snapshots/5f1b8cd7/model.onnx": {"size": 1, "blob_id": "a"}})
    path = _metadata_file(tmp_path, payload)
    before = path.stat().st_mtime_ns

    embeddings._normalize_cache_metadata(str(tmp_path))

    assert path.read_text(encoding="utf-8") == payload
    assert path.stat().st_mtime_ns == before, "an untouched cache must not be rewritten"


def test_building_the_client_repairs_the_cache_it_is_about_to_verify(tmp_path, monkeypatch):
    path = _metadata_file(tmp_path, json.dumps(WINDOWS_KEYS))
    monkeypatch.setattr(embeddings, "FASTEMBED_CACHE_DIR", str(tmp_path))

    embeddings.FastEmbedClient()

    assert all("\\" not in k for k in json.loads(path.read_text(encoding="utf-8"))), (
        "the repair must run before fastembed verifies the cache, or the first "
        "start after a Windows run still logs the bogus mismatch"
    )


def test_unreadable_metadata_never_breaks_startup(tmp_path):
    path = _metadata_file(tmp_path, "{not json")

    embeddings._normalize_cache_metadata(str(tmp_path))  # must not raise

    assert path.read_text(encoding="utf-8") == "{not json"