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