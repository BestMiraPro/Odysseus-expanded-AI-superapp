# tests/test_llm_message_text.py
"""_openai_message_text: final-text selection from an OpenAI-format message.

Reasoning models on OpenAI-compatible providers (W&B Inference, vLLM
--reasoning-parser, NIM, Ollama) return chain-of-thought in a side field
whose name varies by provider, and `content` can come back empty when the
completion budget is exhausted mid-think. The helper must prefer `content`
but fall back through the known reasoning field names so callers see what
the model said instead of an empty string.
"""

from src.llm_core import _openai_message_text


def test_content_preferred_over_reasoning():
    msg = {"content": '[{"q": 1}]', "reasoning": "thinking..."}
    assert _openai_message_text(msg) == '[{"q": 1}]'


def test_falls_back_to_reasoning_content():
    msg = {"content": "", "reasoning_content": "the answer"}
    assert _openai_message_text(msg) == "the answer"


def test_falls_back_to_reasoning_field():
    # W&B Inference (Kimi-K2.6) puts chain-of-thought in `reasoning`.
    msg = {"content": "", "reasoning": "step 1..."}
    assert _openai_message_text(msg) == "step 1..."


def test_falls_back_to_thinking_field():
    msg = {"content": None, "thinking": "hmm"}
    assert _openai_message_text(msg) == "hmm"


def test_whitespace_content_treated_as_empty():
    msg = {"content": "   \n", "reasoning": "real text"}
    assert _openai_message_text(msg) == "real text"


def test_content_block_list_joined():
    msg = {"content": [{"type": "text", "text": "part 1 "},
                       {"type": "text", "text": "part 2"}]}
    assert _openai_message_text(msg) == "part 1 part 2"


def test_empty_message():
    assert _openai_message_text({}) == ""
    assert _openai_message_text(None) == ""
