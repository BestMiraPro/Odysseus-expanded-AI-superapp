"""Tests for is_vision_model (issue #124).

Local vision models served through Ollama/llama.cpp show up under many
names. If one isn't recognized as vision-capable, the image attachment is
stripped from the request before it reaches the model, so it silently never
sees the picture.
"""
from src.chat_helpers import is_vision_model


def test_recognizes_local_and_hosted_vision_models():
    for name in [
        # the ones #124 missed
        "moondream", "moondream:latest",
        "llama3.2-vision:11b", "granite3.2-vision",
        "qwen2.5-vl:7b", "qwen2.5vl", "internvl2.5", "cogvlm",
        # already worked, keep them working
        "llava", "llava:7b", "bakllava", "minicpm-v",
        "gpt-4o", "claude-sonnet-4", "gemini-2.0-flash", "pixtral-12b",
    ]:
        assert is_vision_model(name), f"{name!r} should be detected as vision-capable"


def test_text_only_models_not_flagged():
    for name in ["qwen2.5:3b", "mistral", "llama3.1:8b", "deepseek-r1", "phi3", "vicuna", ""]:
        assert not is_vision_model(name), f"{name!r} should not be flagged as vision"


def test_none_is_safe():
    assert is_vision_model(None) is False


def test_recognizes_multimodal_families_without_vision_in_name():
    # issue #1274: these are vision-capable but their names don't contain
    # "vision"/"vl", so they were dropped and the model never saw the image.
    for name in [
        "gemma3:4b", "gemma3", "gemma-3-27b-it",
        "llama4:scout", "llama4", "llama-4-maverick",
        "mistral-small3.1", "mistral-small-3.2",
        "phi-4-multimodal", "phi4-multimodal",
    ]:
        assert is_vision_model(name), f"{name!r} should be detected as vision-capable"


def test_new_keywords_do_not_overmatch_text_models():
    # The added families must not flag their text-only siblings.
    for name in ["gemma2:9b", "gemma:7b", "llama3.3", "mistral-small", "phi-3-mini"]:
        assert not is_vision_model(name), f"{name!r} should not be flagged as vision"


def test_recognizes_current_hosted_vision_models():
    # GPT-5.x, o3/o4, Grok 4, and every Claude 3+ id shape (legacy
    # "claude-3-5-sonnet-*" puts the version before the family, newer ids like
    # "claude-fable-5-1" use a family the keyword list never had).
    for name in [
        "gpt-5", "gpt-5.1", "gpt-5-mini", "openai/gpt-5.2",
        "o3", "o3-pro", "o3-2025-04-16", "openai/o3", "o4-mini",
        "grok-4", "grok-4-fast", "x-ai/grok-4.1",
        "claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5", "claude-sonnet-5-5",
        "claude-haiku-4-5", "claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
        "claude-3-haiku-20240307", "anthropic/claude-3.7-sonnet",
        "anthropic.claude-opus-4-7",
    ]:
        assert is_vision_model(name), f"{name!r} should be detected as vision-capable"


def test_current_hosted_rules_do_not_overmatch_text_only_models():
    for name in [
        "o3-mini", "o1-mini", "claude-2.1", "claude-2", "claude-instant-1.2",
        "qwen3:8b", "gpt-oss:20b", "grok-3-mini", "grok-code-fast-1",
        "deepseek-v3", "kimi-k2", "command-r",
    ]:
        assert not is_vision_model(name), f"{name!r} should not be flagged as vision"
