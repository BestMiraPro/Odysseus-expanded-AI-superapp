"""OpenCode Zen / Go detection must key off host opencode.ai plus the path.

``_host_match(url, "opencode.ai/zen/go")`` compared the URL's hostname with a
string carrying a path, which can never be equal, so both gateways were
always classified as generic "openai" and labelled with the bare host.
"""
import pytest

from src import llm_core
from src.endpoint_resolver import build_chat_url, build_headers


@pytest.mark.parametrize("url, provider, label", [
    ("https://opencode.ai/zen/go/v1", "opencode-go", "OpenCode Go"),
    ("https://opencode.ai/zen/go/v1/chat/completions", "opencode-go", "OpenCode Go"),
    ("https://opencode.ai/zen/go", "opencode-go", "OpenCode Go"),
    ("https://opencode.ai/zen/v1", "opencode-zen", "OpenCode Zen"),
    ("https://opencode.ai/zen/v1/chat/completions", "opencode-zen", "OpenCode Zen"),
    ("https://OpenCode.ai/zen/", "opencode-zen", "OpenCode Zen"),
])
def test_opencode_gateways_detected(url, provider, label):
    assert llm_core._detect_provider(url) == provider
    assert llm_core._provider_label(url) == label


@pytest.mark.parametrize("url", [
    "https://opencode.ai",                       # no gateway path
    "https://opencode.ai/docs/zen",              # /zen not a path prefix
    "https://opencode.ai/zenith/v1",             # look-alike segment
    "https://proxy.example.com/opencode.ai/zen/v1",  # domain only in the path
    "https://opencode.ai.evil.example/zen/v1",   # look-alike host
])
def test_non_gateway_urls_are_not_opencode(url):
    assert llm_core._detect_provider(url) == "openai"


def test_opencode_stays_on_openai_compatible_wire(monkeypatch):
    import src.endpoint_resolver as er

    monkeypatch.setattr(er, "resolve_url", lambda u: u)
    assert build_chat_url("https://opencode.ai/zen/go/v1") == "https://opencode.ai/zen/go/v1/chat/completions"
    assert build_headers("sk-test", "https://opencode.ai/zen/v1") == {"Authorization": "Bearer sk-test"}
    assert llm_core._is_self_hosted_openai_compatible("https://opencode.ai/zen/v1") is False
