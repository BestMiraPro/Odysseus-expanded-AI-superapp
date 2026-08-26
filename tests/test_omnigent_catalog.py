"""Worker capability/cost metadata for the Omnigent crew orchestrator.

The orchestrator routes real work on this text, so the load-bearing property is
that unknown stays unknown: a fabricated price or benchmark would be worse than
no metadata at all, because it looks authoritative.
"""
import json

import pytest

from src.omnigent_catalog import (
    capability_line,
    crew_prompt,
    describe_model,
    dispatch_guidance,
    family_of,
    load_declared,
)


# ── derived facts ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "model_id,params,active,moe",
    [
        ("Qwen/Qwen3-Coder-480B-A35B-Instruct", 480, 35, True),
        ("nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B", 550, 55, True),
        ("meta-llama/Llama-3.1-8B-Instruct", 8, None, False),
        ("Qwen/Qwen3.8-27B", 27, None, False),
        ("zai-org/GLM-5.2", None, None, False),  # no scale in the id
    ],
)
def test_parameter_scale_is_read_from_the_id(model_id, params, active, moe):
    d = describe_model(model_id, declared={})
    assert d["params_b"] == params
    assert d["active_params_b"] == active
    assert d["moe"] is moe


@pytest.mark.parametrize(
    "model_id,tier",
    [
        ("meta-llama/Llama-3.1-8B-Instruct", "small"),
        ("Qwen/Qwen3.8-27B", "mid"),
        ("Qwen/Qwen3-235B-A22B-Thinking-2507", "large"),
        ("Qwen/Qwen3-Coder-480B-A35B-Instruct", "frontier"),
        ("zai-org/GLM-5.2", "unknown"),
    ],
)
def test_size_tiers(model_id, tier):
    assert describe_model(model_id, declared={})["tier"] == tier


@pytest.mark.parametrize(
    "model_id,trait",
    [
        ("Qwen/Qwen3-Coder-480B-A35B-Instruct", "code"),
        ("moonshotai/Kimi-K2.7-Code", "code"),
        ("Qwen/Qwen3-235B-A22B-Thinking-2507", "reasoning"),
        ("deepseek-ai/DeepSeek-V4-Flash", "fast"),
        ("nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B", "heavyweight"),
    ],
)
def test_specialisation_markers(model_id, trait):
    assert trait in describe_model(model_id, declared={})["traits"]


def test_family_is_the_provider_prefix():
    assert family_of("zai-org/GLM-5.2") == "zai-org"
    assert family_of("bare-model-name") == "bare-model-name"


# ── unknown stays unknown ─────────────────────────────────────────────────

def test_cost_is_reported_unknown_rather_than_guessed():
    line = capability_line("zai-org/GLM-5.2", declared={})
    assert "cost unknown" in line
    d = describe_model("zai-org/GLM-5.2", declared={})
    assert d["cost_known"] is False
    assert d["input_per_mtok"] is None
    assert d["benchmarks"] == {}


def test_no_benchmarks_are_claimed_without_declared_data():
    for mid in ("zai-org/GLM-5.2", "Qwen/Qwen3-Coder-480B-A35B-Instruct"):
        assert "benchmark" not in capability_line(mid, declared={}).lower()


# ── declared data ─────────────────────────────────────────────────────────

def test_declared_pricing_and_benchmarks_reach_the_line():
    declared = {
        "zai-org/GLM-5.2": {
            "input_per_mtok": 0.6,
            "output_per_mtok": 2.2,
            "benchmarks": {"swe-bench-verified": 64.2},
            "notes": "long-context refactors",
        }
    }
    line = capability_line("zai-org/GLM-5.2", declared)
    assert "USD 0.6/M in" in line and "2.2/M out" in line
    assert "swe-bench-verified 64.2" in line
    assert "long-context refactors" in line
    assert "cost unknown" not in line


def test_missing_cost_file_is_not_an_error(tmp_path):
    assert load_declared(str(tmp_path / "nope.json")) == {}


def test_malformed_cost_file_degrades_instead_of_breaking_launch(tmp_path):
    p = tmp_path / "costs.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert load_declared(str(p)) == {}


def test_non_object_cost_file_is_ignored(tmp_path):
    p = tmp_path / "costs.json"
    p.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert load_declared(str(p)) == {}


# ── dispatch rules ────────────────────────────────────────────────────────

def test_orchestrator_is_told_not_to_delegate_to_its_own_model():
    g = dispatch_guidance("zai-org/GLM-5.2", ["zai-org/GLM-5.2", "meta-llama/Llama-3.1-8B-Instruct"])
    assert "Do NOT delegate to a worker running that same model" in g


def test_same_provider_workers_are_called_out_by_name():
    g = dispatch_guidance(
        "zai-org/GLM-5.2",
        ["zai-org/GLM-5.2", "zai-org/GLM-5.1", "meta-llama/Llama-3.1-8B-Instruct"],
    )
    assert "share your provider (zai-org)" in g
    assert "zai-org/GLM-5.1" in g
    # The point of the rule: same provider means the same rate limit.
    assert "rate limit" in g
    assert "meta-llama/Llama-3.1-8B-Instruct" not in g.split("share your provider")[1].split("\n")[0]


def test_no_same_provider_line_when_rosters_do_not_overlap():
    g = dispatch_guidance("zai-org/GLM-5.2", ["meta-llama/Llama-3.1-8B-Instruct"])
    assert "share your provider" not in g


def test_subscription_harnesses_are_told_to_conserve_their_budget():
    g = dispatch_guidance("zai-org/GLM-5.2", [])
    assert "subscription harness" in g and "synthesis" in g


def test_guidance_survives_an_unknown_orchestrator():
    g = dispatch_guidance(None, ["meta-llama/Llama-3.1-8B-Instruct"])
    assert "Dispatch rules:" in g
    assert "Do NOT delegate" not in g


# ── the assembled prompt ──────────────────────────────────────────────────

def test_crew_prompt_lists_every_worker_with_its_capabilities():
    ids = ["meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen3-Coder-480B-A35B-Instruct"]
    slugs = ["llama-8b", "qwen-coder"]
    p = crew_prompt(slugs, ids, "zai-org/GLM-5.2", declared={})
    for slug in slugs:
        assert f"- {slug}:" in p
    assert "tier=small" in p and "tier=frontier" in p
    assert "good for: code" in p
    assert "Dispatch rules:" in p


# ── operator policy: supersession and a parameter floor ───────────────────
# Judgements the derived metadata cannot reach. Someone compared the
# benchmarks; the orchestrator just applies the conclusion.

_DECLARED = {
    "_policy": {"min_params_b": 20},
    "deepseek-ai/DeepSeek-V4-Flash-0731": {"input_per_mtok": 0.13},
    "deepseek-ai/DeepSeek-V4-Flash": {
        "input_per_mtok": 0.14,
        "superseded_by": "deepseek-ai/DeepSeek-V4-Flash-0731",
    },
    "deepseek-ai/DeepSeek-V4-Pro": {
        "input_per_mtok": 1.15,
        "params_b": 1600,
        "superseded_by": "deepseek-ai/DeepSeek-V4-Flash-0731",
    },
    "ibm-granite/granite-4.1-8b": {"input_per_mtok": 0.05},
}


def test_superseded_workers_are_marked_in_their_own_line():
    line = capability_line("deepseek-ai/DeepSeek-V4-Pro", _DECLARED)
    assert line.startswith("SUPERSEDED by deepseek-ai/DeepSeek-V4-Flash-0731 — do not use")


def test_superseded_workers_are_named_with_their_replacement_in_the_rules():
    g = dispatch_guidance(
        "zai-org/GLM-5.2",
        ["deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash-0731"],
        _DECLARED,
    )
    assert "Never dispatch to a SUPERSEDED worker" in g
    assert "deepseek-ai/DeepSeek-V4-Pro (use deepseek-ai/DeepSeek-V4-Flash-0731)" in g


def test_the_replacement_itself_is_never_flagged():
    g = dispatch_guidance(
        None, ["deepseek-ai/DeepSeek-V4-Flash-0731"], _DECLARED
    )
    assert "SUPERSEDED" not in g
    assert "do not use" not in capability_line(
        "deepseek-ai/DeepSeek-V4-Flash-0731", _DECLARED
    )


def test_parameter_floor_excludes_only_models_known_to_be_small():
    g = dispatch_guidance(
        None,
        [
            "ibm-granite/granite-4.1-8b",            # 8B  -> excluded
            "meta-llama/Llama-3.1-8B-Instruct",      # 8B  -> excluded
            "openai/gpt-oss-20b",                    # 20B -> at the floor, kept
            "deepseek-ai/DeepSeek-V4-Pro",           # 1600B -> kept
        ],
        _DECLARED,
    )
    line = [l for l in g.splitlines() if "below 20B" in l][0]
    assert "granite-4.1-8b" in line
    assert "Llama-3.1-8B-Instruct" in line
    assert "gpt-oss-20b" not in line          # exactly at the floor is not below it
    assert "DeepSeek-V4-Pro" not in line


def test_unknown_size_is_not_treated_as_small():
    """Regression: `params_b or 0` excluded every model whose id carries no
    parameter count — including DeepSeek-V4-Flash-0731, the model the operator
    had explicitly marked as preferred."""
    g = dispatch_guidance(
        None,
        ["deepseek-ai/DeepSeek-V4-Flash-0731", "zai-org/GLM-5.2"],
        _DECLARED,
    )
    small = [l for l in g.splitlines() if "below 20B" in l]
    assert not small, f"unknown-size models must not be excluded: {small}"


def test_policy_rules_are_absent_when_no_policy_is_declared():
    g = dispatch_guidance(None, ["ibm-granite/granite-4.1-8b"], {})
    assert "below" not in g
    assert "SUPERSEDED" not in g


# ── benchmarks ────────────────────────────────────────────────────────────

_BENCH = {
    "_policy": {"benchmarks_shown": ["terminal-bench-v2.1", "non-hallucination-rate"]},
    "deepseek-ai/DeepSeek-V4-Flash-0731": {
        "benchmarks": {
            "terminal-bench-v2.1": 79, "gpqa-diamond": 91,
            "non-hallucination-rate": 8, "scicode": 50,
        }
    },
    "MiniMaxAI/MiniMax-M3": {
        "benchmarks": {"terminal-bench-v2.1": 65, "non-hallucination-rate": 82}
    },
}


def test_only_the_selected_benchmarks_render_in_a_worker_line():
    line = capability_line("deepseek-ai/DeepSeek-V4-Flash-0731", _BENCH)
    assert "terminal-bench-v2.1 79" in line
    assert "non-hallucination-rate 8" in line
    # stored but not selected — a line carrying every board is one nobody reads
    assert "gpqa-diamond" not in line
    assert "scicode" not in line


def test_selected_benchmarks_keep_their_declared_order():
    line = capability_line("deepseek-ai/DeepSeek-V4-Flash-0731", _BENCH)
    assert line.index("terminal-bench-v2.1") < line.index("non-hallucination-rate")


def test_hallucination_rule_names_the_worst_and_best_workers():
    g = dispatch_guidance(
        None,
        ["deepseek-ai/DeepSeek-V4-Flash-0731", "MiniMaxAI/MiniMax-M3"],
        _BENCH,
    )
    assert "non-hallucination-rate before trusting a factual claim" in g
    assert "deepseek-ai/DeepSeek-V4-Flash-0731 (8%)" in g
    assert "highest: MiniMaxAI/MiniMax-M3 (82%)" in g


def test_no_hallucination_rule_without_the_data():
    g = dispatch_guidance(None, ["zai-org/GLM-5.2"], {})
    assert "non-hallucination" not in g


# ── worker selection ──────────────────────────────────────────────────────
# The roster is capped, so which models fill it matters. Taking the provider's
# cache order filled slots with deprecated and unmeasured models while the ones
# with benchmark data went unused.

from src.omnigent_catalog import select_workers  # noqa: E402

_SEL = {
    "_policy": {"min_params_b": 20, "benchmarks_shown": ["terminal-bench-v2.1"]},
    "good-measured/A": {"benchmarks": {"terminal-bench-v2.1": 79}, "params_b": 100},
    "good-measured/B": {"benchmarks": {"terminal-bench-v2.1": 65}, "params_b": 100},
    "big-unmeasured/C": {"params_b": 500},
    "small-unmeasured/D": {"params_b": 8},
    "old/E": {"deprecated": True, "benchmarks": {"terminal-bench-v2.1": 99}, "params_b": 200},
    "dominated/F": {"superseded_by": "good-measured/A", "params_b": 200},
}
_ALL = ["small-unmeasured/D", "old/E", "dominated/F", "big-unmeasured/C",
        "good-measured/B", "good-measured/A"]


def test_measured_models_are_preferred_and_ranked_by_benchmark():
    picked = select_workers(_ALL, _SEL, limit=3)
    assert picked[:2] == ["good-measured/A", "good-measured/B"]
    assert picked[2] == "big-unmeasured/C"


def test_deprecated_superseded_and_undersized_models_are_excluded():
    picked = select_workers(_ALL, _SEL, limit=10)
    assert "old/E" not in picked, "deprecated, even with the top benchmark"
    assert "dominated/F" not in picked
    assert "small-unmeasured/D" not in picked


def test_selection_respects_the_limit():
    assert len(select_workers(_ALL, _SEL, limit=2)) == 2


def test_selection_falls_back_rather_than_returning_an_empty_roster():
    """A poor roster still beats no workers at all."""
    everything_excluded = {
        "_policy": {"min_params_b": 20},
        "x/tiny": {"params_b": 1},
        "y/tiny": {"params_b": 2},
    }
    picked = select_workers(["x/tiny", "y/tiny"], everything_excluded, limit=5)
    assert picked == ["x/tiny", "y/tiny"]


def test_selection_without_declared_data_keeps_the_input_order():
    ids = ["a/one", "b/two", "c/three"]
    assert select_workers(ids, {}, limit=2) == ids[:2]


def test_hallucination_rule_is_suppressed_when_every_score_is_identical():
    same = {
        "m/one": {"benchmarks": {"non-hallucination-rate": 50}},
        "m/two": {"benchmarks": {"non-hallucination-rate": 50}},
    }
    g = dispatch_guidance(None, ["m/one", "m/two"], same)
    assert "non-hallucination-rate before trusting" not in g, (
        "naming the same worker as both lowest and highest reads as noise"
    )
