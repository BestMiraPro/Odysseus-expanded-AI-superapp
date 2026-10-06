"""Providers that attach cumulative usage to every stream chunk.

W&B Inference (and vLLM with continuous usage stats) put a running
``usage`` on each chunk. stream_llm used to emit a usage event for each
chunk without output, so one call produced several usage events; the agent
loop adds usage events up, so it counted the prompt two or three times
(seen live on W&B: a 6.6k-token prompt billed as 13.2k). stream_llm now
emits exactly one usage event per call, with the final counts.
"""
import json

from tests.test_llm_core_sse_no_space import _drive


def _chunk(delta, completion, finish=None):
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return "data: " + json.dumps({"model": "meta-llama/Llama-3.1-8B-Instruct", "choices": [choice],
                                  "usage": {"prompt_tokens": 40, "completion_tokens": completion,
                                            "total_tokens": 40 + completion}})


def test_cumulative_usage_is_emitted_once_with_final_counts(monkeypatch):
    lines = [
        _chunk({"role": "assistant", "content": ""}, 0),
        _chunk({"content": "ok"}, 1),
        _chunk({}, 2, finish="stop"),
        "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 2,
                                                       "total_tokens": 42}}),
        "data: [DONE]",
    ]
    blob = _drive(monkeypatch, "https://api.inference.wandb.ai/v1", lines, "meta-llama/Llama-3.1-8B-Instruct")
    usages = [json.loads(ln[6:])["data"] for ln in blob.split("\n")
              if ln.startswith("data: {") and '"type": "usage"' in ln]
    assert len(usages) == 1, usages
    assert (usages[0]["input_tokens"], usages[0]["output_tokens"]) == (40, 2)
    assert "ok" in blob


def test_stream_without_done_still_reports_usage(monkeypatch):
    lines = [_chunk({"content": "hi"}, 1), _chunk({}, 3, finish="stop")]
    blob = _drive(monkeypatch, "https://api.inference.wandb.ai/v1", lines, "m")
    usages = [ln for ln in blob.split("\n") if '"type": "usage"' in ln]
    assert len(usages) == 1 and '"output_tokens": 3' in usages[0]
