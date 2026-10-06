"""Pin Claude pricing / context lookups in chatRenderer.js MODEL_INFO.

Prices are per 1M tokens from platform.claude.com/docs/en/about-claude/pricing.
Regressions covered:
  * claude-opus-4-6 was billed at $15/$75 (actual $5/$25);
  * the bare 'claude-opus-4' key caught Opus 4.5 / 4.7 / 4.8 at $15/$75;
  * 'claude-haiku-4' billed Haiku 4.5 at $0.80/$4 (actual $1/$5);
  * Claude 5.x models (Opus 5.5, Sonnet 5.5, Fable 5.1) had no pricing;
  * OpenRouter-style dotted ids fell back to the older family key;
  * prompt-cache reads/writes were priced at the full input rate.

Driven through node against the real MODEL_INFO block, getModelCost source and
matchKey.js module (chatRenderer.js itself pulls in browser-only modules).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "static" / "js" / "chatRenderer.js").read_text(encoding="utf-8")
_MATCH = _REPO / "static" / "js" / "model" / "matchKey.js"
_HAS_NODE = shutil.which("node") is not None


def _run(expr: str):
    info = re.search(r"^const MODEL_INFO = \{.*?^\};", _SRC, re.MULTILINE | re.DOTALL)
    cost = re.search(r"^export function getModelCost\(.*?^\}", _SRC, re.MULTILINE | re.DOTALL)
    assert info and cost
    js = "\n".join([
        f"import {{ matchModelKey }} from '{_MATCH.as_uri()}';",
        info.group(0),
        "const MODEL_PRICING = MODEL_INFO;",
        cost.group(0).replace("export function", "function", 1),
        "function info(name) { const k = matchModelKey(name, Object.keys(MODEL_INFO)); "
        "return k ? { key: k, ...MODEL_INFO[k] } : null; }",
        f"console.log(JSON.stringify({expr}));",
    ])
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _price(name):
    got = _run(f"info({json.dumps(name)})")
    return got and (got["key"], got["input"], got["output"], got["ctx"])


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
@pytest.mark.parametrize("name, expected", [
    ("claude-fable-5-1", ("claude-fable-5-1", 10.0, 50.0, 1000000)),
    ("claude-opus-5-5", ("claude-opus-5-5", 4.0, 20.0, 1000000)),
    ("claude-opus-5", ("claude-opus-5", 5.0, 25.0, 1000000)),
    ("claude-opus-4-8", ("claude-opus-4-8", 5.0, 25.0, 1000000)),
    ("claude-opus-4-7", ("claude-opus-4-7", 5.0, 25.0, 1000000)),
    ("claude-opus-4-6", ("claude-opus-4-6", 5.0, 25.0, 1000000)),
    ("claude-opus-4-5-20251101", ("claude-opus-4-5", 5.0, 25.0, 200000)),
    ("anthropic/claude-opus-4.5", ("claude-opus-4-5", 5.0, 25.0, 200000)),
    ("claude-opus-4-1-20250805", ("claude-opus-4-1", 15.0, 75.0, 200000)),
    ("claude-opus-4-20250514", ("claude-opus-4", 15.0, 75.0, 200000)),
    ("claude-sonnet-5-5", ("claude-sonnet-5-5", 2.0, 10.0, 1000000)),
    ("claude-sonnet-5", ("claude-sonnet-5", 2.0, 10.0, 1000000)),
    ("claude-sonnet-4-6", ("claude-sonnet-4-6", 3.0, 15.0, 1000000)),
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", ("claude-sonnet-4-5", 3.0, 15.0, 200000)),
    ("claude-haiku-4-5", ("claude-haiku-4-5", 1.0, 5.0, 200000)),
    ("claude-haiku-4-5-20251001", ("claude-haiku-4-5", 1.0, 5.0, 200000)),
    ("claude-3-5-haiku-20241022", ("claude-3-5-haiku", 0.8, 4.0, 200000)),
])
def test_claude_models_resolve_to_most_specific_verified_price(name, expected):
    assert tuple(_price(name)) == expected


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_dotted_ids_keep_non_claude_keys():
    assert _price("gpt-4.1-mini")[0] == "gpt-4.1-mini"
    assert _price("meta-llama/llama-3.3-70b")[0] == "llama-3.3"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_cache_tokens_use_cache_rates():
    # 1M input of which 900k cache reads + 100k cache writes, no output.
    # Sonnet 4.6: reads 0.1x $3 = $0.30/M, 5-minute writes 1.25x $3 = $3.75/M.
    got = _run("getModelCost('claude-sonnet-4-6', 1000000, 0, {read: 900000, write: 100000})")
    assert got == pytest.approx(0.9 * 0.30 + 0.1 * 3.75)
    # Opus 5.5 cache reads are $0.20/M (0.05x), Fable 5.1 $0.25/M (0.025x).
    assert _run("getModelCost('claude-opus-5-5', 1000000, 0, {read: 1000000})") == pytest.approx(0.20)
    assert _run("getModelCost('claude-fable-5-1', 1000000, 0, {read: 1000000})") == pytest.approx(0.25)
    # Without cache info the full input rate applies (unchanged behaviour).
    assert _run("getModelCost('claude-haiku-4-5', 1000000, 1000000)") == pytest.approx(6.0)


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_server_prices_override_the_name_matched_table():
    # A model the budget bills as unpriced (Kimi on W&B) shows no cost, even
    # though the built-in table name-matches the vendor's own price.
    base = _run("getModelCost('moonshotai/Kimi-K2.6', 1000, 1000)")
    assert base is not None and base > 0
    unpriced = _run("(globalThis.__odyServerPrices = {'moonshotai/kimi-k2.6': {unpriced: true}},"
                    " getModelCost('moonshotai/Kimi-K2.6', 1000, 1000))")
    assert unpriced is None
    declared = _run("(globalThis.__odyServerPrices = {'moonshotai/kimi-k2.6': {input: 1, output: 3, source: 'declared'}},"
                    " getModelCost('moonshotai/Kimi-K2.6', 1000000, 1000000))")
    assert declared == 4.0
