"""Anthropic prompt-cache tokens must count toward reported input usage.

Anthropic's ``usage.input_tokens`` excludes ``cache_read_input_tokens`` and
``cache_creation_input_tokens``. The stream adapter used to log the cached
counts and drop them, so with prompt caching on (every agentic/tool call and
any system prompt > 4000 chars) input tokens, context_percent and cost were
all understated.
"""
import asyncio
import json

from src import llm_core


class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _anthropic_usage(monkeypatch, start_usage, delta_usage):
    lines = [
        "data: " + json.dumps({
            "type": "message_start",
            "message": {"model": "claude-sonnet-4-6", "usage": start_usage},
        }),
        "data: " + json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }),
        "data: " + json.dumps({"type": "message_delta", "usage": delta_usage}),
        "data: " + json.dumps({"type": "message_stop"}),
    ]
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)

    async def run():
        return [
            chunk
            async for chunk in llm_core._stream_llm_inner(
                "https://api.anthropic.com/v1/messages",
                "claude-sonnet-4-6",
                [{"role": "user", "content": "hi"}],
                headers={"x-api-key": "k"},
            )
        ]

    chunks = asyncio.run(run())
    return [
        json.loads(c[6:])["data"]
        for c in chunks
        if c.startswith("data: ") and '"type": "usage"' in c
    ]


def test_cached_tokens_are_added_to_input_and_exposed(monkeypatch):
    usage = _anthropic_usage(
        monkeypatch,
        {"input_tokens": 12, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 300},
        {"output_tokens": 5},
    )
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 12 + 9000 + 300
    assert usage[0]["output_tokens"] == 5
    assert usage[0]["cache_read_input_tokens"] == 9000
    assert usage[0]["cache_creation_input_tokens"] == 300


def test_no_cache_usage_keeps_plain_shape(monkeypatch):
    usage = _anthropic_usage(
        monkeypatch,
        {"input_tokens": 4, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"output_tokens": 1},
    )
    assert usage[0]["input_tokens"] == 4
    assert "cache_read_input_tokens" not in usage[0]
    assert "cache_creation_input_tokens" not in usage[0]


def test_cumulative_message_delta_counts_win(monkeypatch):
    usage = _anthropic_usage(
        monkeypatch,
        {"input_tokens": 3},
        {"output_tokens": 7, "input_tokens": 3, "cache_read_input_tokens": 500},
    )
    assert usage[0]["input_tokens"] == 503
    assert usage[0]["cache_read_input_tokens"] == 500


def test_helper_ignores_malformed_cache_counts():
    assert llm_core._anthropic_usage_counts(10, 2, "bogus", None) == {
        "input_tokens": 10,
        "output_tokens": 2,
    }
    assert llm_core._anthropic_usage_counts("bad", 2, 5, 5) is None


def test_agent_usage_bucket_keeps_real_cache_counts_for_pricing():
    from src.agent_loop import _usage_bucket

    common = dict(
        round_num=1, model="claude-sonnet-4-6", endpoint_id="e", endpoint_label="E",
        endpoint_cost_tracked=True, input_tokens=9312, output_tokens=5,
    )
    real = _usage_bucket(
        **common, usage_source="real",
        cache_read_input_tokens=9000, cache_creation_input_tokens=300,
    )
    assert real["input_tokens"] == 9312
    assert real["cache_read_input_tokens"] == 9000
    assert real["cache_creation_input_tokens"] == 300
    # Estimated rounds and zero counts carry no cache fields.
    est = _usage_bucket(**common, usage_source="estimated", cache_read_input_tokens=9000)
    assert "cache_read_input_tokens" not in est
    plain = _usage_bucket(**common, usage_source="real")
    assert "cache_read_input_tokens" not in plain and "cache_creation_input_tokens" not in plain
