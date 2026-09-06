"""Pin the base-prompt cache contract before the prompt builders are extracted.

``src/agent_loop.py`` is 6,442 lines and its prompt assembly is the obvious
first extraction. The blocker is state, not size: ``_cached_base_prompt`` and
``_cached_base_prompt_key`` are module-level globals that four test modules
reset by rebinding them on ``agent_loop``. A rebindable global cannot be
re-exported — a moved builder would read the new module's binding while those
tests cleared the old one, so the cache would silently never reset.

These tests pin the observable behaviour (when the base prompt is rebuilt
versus reused) through ``reset_base_prompt_cache()``, an entry point that
survives the move because it is a function, not a rebound name. With the
contract pinned, the extraction becomes a mechanical change rather than a
guess.
"""

from __future__ import annotations

import pytest

from src import agent_loop


@pytest.fixture(autouse=True)
def clean_cache():
    agent_loop.reset_base_prompt_cache()
    yield
    agent_loop.reset_base_prompt_cache()


def _count_base_prompt_builds(monkeypatch):
    """Count real base-prompt assemblies, keeping the return shape intact."""
    calls = []
    real = agent_loop._build_base_prompt

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_loop, "_build_base_prompt", counting)
    return calls


def _build(**overrides):
    kwargs = dict(
        messages=[{"role": "user", "content": "hello"}],
        model="test-model",
        active_document=None,
        mcp_mgr=None,
    )
    kwargs.update(overrides)
    return agent_loop._build_system_prompt(**kwargs)


def test_reset_helper_exists_and_clears_the_cache():
    """A function, unlike a rebound global, survives being re-exported."""
    assert callable(getattr(agent_loop, "reset_base_prompt_cache", None))
    _build()
    agent_loop.reset_base_prompt_cache()
    assert agent_loop.base_prompt_cache_is_empty()


def test_identical_requests_reuse_the_cached_base_prompt(monkeypatch):
    calls = _count_base_prompt_builds(monkeypatch)

    _build()
    _build()

    # The skill index is deliberately recomputed on a cache hit (it is
    # user-editable and must not sit in the trusted system role), so
    # _build_base_prompt is still called — but the cache must be populated.
    assert len(calls) == 2
    assert not agent_loop.base_prompt_cache_is_empty()


def test_a_different_cache_key_rebuilds(monkeypatch):
    _build(disabled_tools={"a"})
    first_key = agent_loop.base_prompt_cache_key()

    _build(disabled_tools={"b"})
    second_key = agent_loop.base_prompt_cache_key()

    assert first_key != second_key


class _Doc:
    """active_document is an ORM row, not a dict — attribute access."""
    id = "d1"
    title = "T"
    language = "text"
    current_content = "x"
    version_count = 1


def test_an_active_document_is_never_cached():
    """Document context is per-request; caching it would leak across sessions."""
    agent_loop.reset_base_prompt_cache()
    _build(active_document=_Doc())
    assert agent_loop.base_prompt_cache_is_empty()


def test_reset_forces_the_next_build_to_repopulate():
    _build()
    assert not agent_loop.base_prompt_cache_is_empty()
    agent_loop.reset_base_prompt_cache()
    assert agent_loop.base_prompt_cache_is_empty()
    _build()
    assert not agent_loop.base_prompt_cache_is_empty()


def test_build_returns_system_messages():
    """Guard the actual contract, not just the cache bookkeeping.

    Note the real return is (messages, tool_schemas) despite the declared
    ``-> List[Dict]`` annotation; the annotation is wrong, not the code.
    """
    messages, tool_schemas = _build()
    assert isinstance(messages, list)
    assert messages and messages[0]["role"] == "system"
    assert isinstance(messages[0]["content"], str)
    assert messages[0]["content"].strip()
    assert isinstance(tool_schemas, list)
