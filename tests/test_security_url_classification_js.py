"""URL provider classification must be parsed-host checks, not substrings
(security plan S3, alerts #1–#6, #202, #211).

Drives the REAL functions under Node:
- `detectProvider` / `setupChatUrlForEndpoint` from static/js/slashCommands.js
  (top-level functions via the repo harness);
- `_normalizeBaseUrl` from static/js/admin.js (nested — brace-extracted);
- the inpainting provider choice from static/js/editor/ai-inpaint.js
  (listener body brace-extracted, DOM stubbed).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._study_js_harness import STUDY_JS, needs_node, run_js

_REPO = Path(__file__).resolve().parent.parent
_SLASH_JS = _REPO / "static" / "js" / "slashCommands.js"
_ADMIN_JS = _REPO / "static" / "js" / "admin.js"
_INPAINT_JS = _REPO / "static" / "js" / "editor" / "ai-inpaint.js"

_HAS_NODE = shutil.which("node") is not None


def _extract_function_block(src: str, marker: str) -> str:
    """Body lines of the function whose declaration line contains `marker`.

    Line-based: the function is assumed to open with `{` on the marker line
    and close with a line that is exactly the opening line's indentation plus
    `}`. Immune to braces inside template literals."""
    lines = src.splitlines()
    idx = next(i for i, ln in enumerate(lines) if marker in ln)
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    for j in range(idx + 1, len(lines)):
        if lines[j] == indent + "}":
            return "\n".join(lines[idx + 1:j])
    raise AssertionError(f"no closing brace for: {marker!r}")


def _extract_listener_body(src: str, marker: str) -> str:
    """Body lines of an addEventListener callback whose registration line
    contains `marker`. Closes at the first `});` line indented at the SAME
    level as the registration line (nested object option-terminators sit
    deeper and must be skipped)."""
    lines = src.splitlines()
    idx = next(i for i, ln in enumerate(lines) if marker in ln)
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    for j in range(idx + 1, len(lines)):
        if lines[j].strip() == "});" \
                and lines[j].startswith(indent) \
                and not lines[j][len(indent):].startswith(" "):
            return "\n".join(lines[idx + 1:j])
    raise AssertionError(f"no listener close for: {marker!r}")


def _run_node(script: str) -> None:
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr or proc.stdout


@needs_node
def _slash_run(epilogue: str) -> dict:
    return run_js("", "detectProvider", "setupChatUrlForEndpoint",
                  epilogue=epilogue, source_path=_SLASH_JS)


def test_slash_detect_provider_uses_exact_ollama_host():
    # HTTPS/URL branch: ollama.com is coerced to the canonical cloud URL
    # only when the PARSED host is exactly ollama.com.
    result = _slash_run(r"""
const cases = {
  'https://ollama.com': 'https://ollama.com/api',
  'https://notollama.com': 'https://notollama.com/v1',
  'https://ollama.com.evil.example': 'https://ollama.com.evil.example/v1',
  'https://evil.example/ollama.com': 'https://evil.example/ollama.com',
  'http://localhost:8000': 'http://localhost:8000/v1',
};
const out = {};
for (const [input, expected] of Object.entries(cases)) {
  const got = detectProvider(input);
  out[input] = got && got.base_url === expected;
}
console.log(JSON.stringify(out));
""")
    assert result == {
        "https://ollama.com": True,
        "https://notollama.com": True,
        "https://ollama.com.evil.example": True,
        "https://evil.example/ollama.com": True,
        "http://localhost:8000": True,
    }, result


def test_slash_chat_url_preserves_custom_endpoints():
    result = _slash_run(r"""
const canonical = setupChatUrlForEndpoint({ base_url: 'https://ollama.com/api', name: '' });
const custom = setupChatUrlForEndpoint({ base_url: 'https://my-ollama.com', name: '' });
const anthropic = setupChatUrlForEndpoint({ base_url: 'https://proxy.example/v1', name: 'Anthropic' });
console.log(JSON.stringify({ canonical, custom, anthropic }));
""")
    assert result["canonical"] == "https://ollama.com/api/chat"
    assert result["custom"] == "https://my-ollama.com/chat/completions", (
        "a custom endpoint whose host merely CONTAINS 'ollama.com' must keep "
        "its own path, not be rewritten to the canonical Ollama cloud"
    )
    assert result["anthropic"] == "https://proxy.example/v1/messages"


def test_admin_base_url_normalization_is_host_exact():
    import json

    body = _extract_function_block(
        _ADMIN_JS.read_text(encoding="utf-8"),
        "function _normalizeBaseUrl",
    )
    cases = {
        "https://ollama.com": "https://ollama.com/api",
        "https://notollama.com": "https://notollama.com/v1",
        "https://my-ollama.com": "https://my-ollama.com/v1",
        "https://ollama.com.evil.example": "https://ollama.com.evil.example/v1",
        "https://evil.example/ollama.com": "https://evil.example/ollama.com",
        "http://localhost:8000": "http://localhost:8000/v1",
        "https://api.openai.com": "https://api.openai.com",
        "https://openrouter.ai": "https://openrouter.ai",
        "https://opencode.ai": "https://opencode.ai",
    }
    script = (
        "function _normalizeBaseUrl(raw) {\n" + body + "\n}\n"
        "const out = {};\n"
        "const cases = " + json.dumps(cases) + ";\n"
        "for (const [input, expected] of Object.entries(cases)) {\n"
        "  out[input] = _normalizeBaseUrl(input) === expected;\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {k: True for k in cases}, result


def test_inpaint_provider_choice_is_host_exact():
    import json

    src = _INPAINT_JS.read_text(encoding="utf-8")
    body = _extract_listener_body(
        src, "getElementById('ge-inpaint-remove').addEventListener")
    factory = (
        "async function runCase(endpoint) {\n"
        "  var els = { 'ge-inpaint-prompt': { value: 'the watermark' },\n"
        "              'ge-strength-slider': { value: '75' } };\n"
        "  var __last = null;\n"
        "  var document = { getElementById: (id) => els[id] || null };\n"
        "  var uiModule = { showToast: () => {} };\n"
        "  var getSelectedAIEndpoint = () => ({ endpoint });\n"
        "  var runInpaint = (args) => { __last = args; };\n"
        + body + "\n"
        "  return __last ? __last.prompt : null;\n"
        "}\n"
    )
    cases = {
        "https://api.openai.com/v1": True,
        "api.openai.com": True,
        "https://api.openai.com@evil.example/v1": False,
        "https://proxy.example/api.openai.com": False,
        "https://openai.my-company.com": False,
    }
    script = (
        factory
        + "\n(async () => {\n"
        + "const out = {};\n"
        + "for (const [endpoint, expectRemove] of Object.entries("
        + json.dumps(cases) + ")) {\n"
        + "  const prompt = await runCase(endpoint) || '';\n"
        + "  out[endpoint] = prompt.startsWith('Remove ') === expectRemove;\n"
        + "}\nconsole.log(JSON.stringify(out));\n"
        + "})().catch((e) => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {k: True for k in cases}, result