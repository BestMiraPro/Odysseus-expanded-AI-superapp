# src/omnigent_catalog.py
"""Capability and cost metadata for Omnigent crew workers.

The crew orchestrator picks which worker gets which task. With nothing but a
list of slugs it picks blind, so it tends to send everything to whichever model
it saw first — expensive for trivial work, and underpowered for hard work.

This module gives it something to choose on. Two sources, kept strictly apart:

**Derived** (always available): facts read out of the model identifier itself —
parameter scale, MoE active-parameter counts, and specialisation markers like
``Coder`` / ``Thinking`` / ``Flash``. These are structural, not opinions, and
they are genuinely predictive of both capability and cost.

**Declared** (optional): real benchmark scores and per-token prices, supplied by
the operator in ``data/omnigent-model-costs.json``. Nothing here ships with
invented numbers. A model with no declared entry is reported as unknown cost
rather than guessed at, because an orchestrator routing on a fabricated price is
worse than one routing on none.

Schema for the optional file — every field optional:

```json
{
  "zai-org/GLM-5.2": {
    "input_per_mtok": 0.6,
    "output_per_mtok": 2.2,
    "currency": "USD",
    "benchmarks": {"swe-bench-verified": 64.2, "aime": 88.0},
    "notes": "strong at long-context refactors"
  }
}
```
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Size tiers by total parameters (billions). Boundaries are deliberately coarse:
# the point is "cheap / mid / heavy / frontier", not false precision.
_TIERS = ((14, "small"), (80, "mid"), (300, "large"))


def _parse_scale(model_id: str) -> dict[str, Any]:
    """Pull parameter counts out of a model id.

    Handles the two shapes in common use:
      ``Qwen3.8-27B``            -> total 27
      ``Qwen3-480B-A35B``        -> total 480, active 35 (MoE)
    """
    total = active = None
    # Active-parameter suffix first (A35B), so the 480B below still matches.
    m_active = re.search(r"[-_]A(\d+(?:\.\d+)?)B\b", model_id, re.I)
    if m_active:
        active = float(m_active.group(1))
    for m in re.finditer(r"(\d+(?:\.\d+)?)B\b", model_id, re.I):
        val = float(m.group(1))
        if active is not None and val == active:
            continue
        total = val if total is None else max(total, val)
    return {"params_b": total, "active_params_b": active, "moe": active is not None}


def _tier(params_b: float | None) -> str:
    if params_b is None:
        return "unknown"
    for ceiling, name in _TIERS:
        if params_b < ceiling:
            return name
    return "frontier"


# Specialisation markers, matched case-insensitively against the id.
_TRAITS: tuple[tuple[str, str], ...] = (
    (r"coder|[-_]code\b", "code"),
    (r"thinking|reason", "reasoning"),
    (r"flash|lightning|mini|instruct-turbo", "fast"),
    (r"\bpro\b|ultra", "heavyweight"),
    (r"vision|vl\b", "vision"),
    (r"embed", "embedding"),
)


def _traits(model_id: str) -> list[str]:
    return [name for pattern, name in _TRAITS if re.search(pattern, model_id, re.I)]


def family_of(model_id: str) -> str:
    """Vendor/family prefix — ``zai-org/GLM-5.2`` -> ``zai-org``.

    Used for the same-roster rule: an orchestrator should not burn its own
    provider's quota on workers drawn from that same provider.
    """
    return (model_id.split("/", 1)[0] if "/" in model_id else model_id).strip().lower()


def _costs_path() -> str:
    from src.constants import DATA_DIR  # local import: avoids a cycle at module load

    return os.path.join(DATA_DIR, "omnigent-model-costs.json")


def load_policy(declared: dict[str, dict] | None = None) -> dict[str, Any]:
    """Operator routing policy from the ``_policy`` key of the cost file.

    Judgements the metadata cannot reach on its own — "this model is strictly
    dominated by that one", "anything under N parameters is not worth a slot".
    They come from someone who has compared the benchmarks; the orchestrator
    just applies them.
    """
    declared = declared if declared is not None else load_declared()
    pol = declared.get("_policy")
    return pol if isinstance(pol, dict) else {}


def load_declared(path: str | None = None) -> dict[str, dict]:
    """Operator-supplied prices/benchmarks. Absent or malformed -> ``{}``.

    Never fatal: routing metadata is an optimisation, and a broken cost file
    must not stop a crew from launching.
    """
    p = path or _costs_path()
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("omnigent cost catalog unreadable (%s): %s", p, exc)
        return {}


def describe_model(model_id: str, declared: dict[str, dict] | None = None) -> dict[str, Any]:
    """Everything known about one model, derived and declared kept separate."""
    declared = declared if declared is not None else load_declared()
    entry = declared.get(model_id) or {}
    scale = _parse_scale(model_id)
    # Declared scale wins: plenty of ids carry no parameter count at all
    # (GLM-5.2, Kimi-K2.7-Code), and without it they land in tier=unknown and
    # the orchestrator loses its main capability signal for exactly the models
    # where it matters most.
    if entry.get("params_b") is not None:
        scale["params_b"] = entry["params_b"]
    if entry.get("active_params_b") is not None:
        scale["active_params_b"] = entry["active_params_b"]
        scale["moe"] = True
    return {
        "model": model_id,
        "family": family_of(model_id),
        "traits": _traits(model_id),
        "tier": _tier(scale["params_b"]),
        **scale,
        "input_per_mtok": entry.get("input_per_mtok"),
        "cached_per_mtok": entry.get("cached_per_mtok"),
        "output_per_mtok": entry.get("output_per_mtok"),
        "currency": entry.get("currency", "USD"),
        "context_k": entry.get("context_k"),
        "deprecated": bool(entry.get("deprecated")),
        "superseded_by": entry.get("superseded_by"),
        "benchmarks": entry.get("benchmarks") or {},
        "notes": entry.get("notes"),
        "cost_known": entry.get("input_per_mtok") is not None,
    }


def _scale_text(d: dict[str, Any]) -> str:
    if d["params_b"] is None:
        return "size unknown"
    if d["moe"] and d["active_params_b"]:
        return f"{d['params_b']:g}B MoE ({d['active_params_b']:g}B active)"
    return f"{d['params_b']:g}B"


def capability_line(
    model_id: str,
    declared: dict[str, dict] | None = None,
    shown: list[str] | None = None,
) -> str:
    """One line describing a worker, for the orchestrator's prompt.

    Reads as a dispatch hint, not a spec sheet — the orchestrator needs to
    decide quickly, and unknowns are stated as unknown.
    """
    declared = declared if declared is not None else load_declared()
    d = describe_model(model_id, declared)
    if shown is None:
        shown = load_policy(declared).get("benchmarks_shown") or []
    bits = [_scale_text(d), f"tier={d['tier']}"]
    if d["context_k"]:
        bits.append(f"{d['context_k']:g}k ctx")
    if d["traits"]:
        bits.append("good for: " + ", ".join(d["traits"]))
    if d["cost_known"]:
        out = d["output_per_mtok"]
        bits.append(
            f"cost {d['currency']} {d['input_per_mtok']:g}/M in"
            + (f", {out:g}/M out" if out is not None else "")
        )
    else:
        bits.append("cost unknown")
    if d["benchmarks"]:
        # Render only the selected boards: a line carrying all eighteen is one
        # the orchestrator skims past, which defeats the point.
        picked = [(k, d["benchmarks"][k]) for k in shown if k in d["benchmarks"]]             if shown else sorted(d["benchmarks"].items())
        if picked:
            bits.append("benchmarks: " + ", ".join(f"{k} {v:g}" for k, v in picked))
    if d["notes"]:
        bits.append(str(d["notes"]))
    if d["superseded_by"]:
        # A strictly-dominated model is worse than a merely expensive one: it
        # costs the same or more AND performs worse, so there is no task for
        # which it is the right answer.
        bits.insert(0, f"SUPERSEDED by {d['superseded_by']} — do not use")
    if d["deprecated"]:
        # Loud, and first thing after the name in practice: a deprecated model
        # can be withdrawn mid-run, which fails a task for reasons the
        # orchestrator would otherwise have no way to anticipate.
        bits.insert(0, "DEPRECATED — avoid unless nothing else fits")
    return " · ".join(bits)


def select_workers(
    model_ids: list[str],
    declared: dict[str, dict] | None = None,
    limit: int = 12,
) -> list[str]:
    """Choose which models become crew workers.

    A roster is capped, so the choice matters: taking the first N in whatever
    order the provider happened to cache them can fill every slot with
    deprecated and unmeasured models while the ones you have data for sit
    unused.

    Excludes what the operator has ruled out — deprecated, superseded, below the
    parameter floor — then ranks what remains: models with declared benchmarks
    first (someone measured them, and the orchestrator can compare them), by
    their leading benchmark, then by scale.
    """
    declared = declared if declared is not None else load_declared()
    policy = load_policy(declared)
    floor = policy.get("min_params_b")
    shown = policy.get("benchmarks_shown") or []

    eligible: list[str] = []
    for mid in model_ids:
        entry = declared.get(mid) or {}
        if entry.get("deprecated") or entry.get("superseded_by"):
            continue
        d = describe_model(mid, declared)
        if floor and d["params_b"] is not None and d["params_b"] < float(floor):
            continue
        eligible.append(mid)

    def rank(mid: str):
        d = describe_model(mid, declared)
        bench = d["benchmarks"]
        lead = next((bench[k] for k in shown if k in bench), None)
        return (
            0 if bench else 1,                    # measured models first
            -(lead if lead is not None else 0),   # then by leading benchmark
            -(d["params_b"] or 0),                # then by scale
            mid,                                  # stable
        )

    # Fall back to the raw list rather than returning nothing if policy excluded
    # everything — a crew with a poor roster still beats a crew with no workers.
    return sorted(eligible, key=rank)[:limit] or model_ids[:limit]


def crew_prompt(
    slugs: list[str],
    model_ids: list[str],
    orchestrator_model: str | None,
    declared: dict[str, dict] | None = None,
) -> str:
    """The crew orchestrator's system prompt: roster + dispatch rules.

    Built here rather than in the route so the roster and the rules that
    reference it stay in one place.
    """
    declared = declared if declared is not None else load_declared()
    roster = "\n".join(
        f"- {slug}: {capability_line(mid, declared)}"
        for slug, mid in zip(slugs, model_ids)
    )
    return (
        "You are the crew orchestrator. Break the goal into scoped tasks, delegate each to "
        "the right worker via sys_session_send, then synthesize their results.\n\n"
        "Available workers:\n"
        f"{roster}\n\n"
        f"{dispatch_guidance(orchestrator_model, model_ids, declared)}"
    )


def _policy_rules(worker_models: list[str], declared: dict[str, dict], policy: dict) -> list[str]:
    """Rules the operator declared, rendered with the workers they affect named."""
    rules: list[str] = []

    superseded = {
        m: declared[m]["superseded_by"]
        for m in worker_models
        if isinstance(declared.get(m), dict) and declared[m].get("superseded_by")
    }
    if superseded:
        pairs = ", ".join(f"{m} (use {by})" for m, by in sorted(superseded.items()))
        rules.append(
            "- Never dispatch to a SUPERSEDED worker — another model in this roster "
            f"beats it on benchmarks at the same or lower price: {pairs}."
        )

    floor = policy.get("min_params_b")
    if floor:
        # Unknown size is NOT small. Several ids carry no parameter count at all,
        # and treating those as zero would exclude exactly the models the
        # operator has taken the trouble to prefer.
        too_small = sorted(
            m for m in worker_models
            if (p := describe_model(m, declared)["params_b"]) is not None
            and p < float(floor)
        )
        if too_small:
            rules.append(
                f"- Do not dispatch to workers below {floor:g}B total parameters — they are "
                "not worth a slot even for simple tasks, because a wrong cheap answer costs "
                f"more to fix than a right one costs to buy: {', '.join(too_small)}."
            )
    return rules


def dispatch_guidance(
    orchestrator_model: str | None,
    worker_models: list[str],
    declared: dict[str, dict] | None = None,
) -> str:
    """Routing rules for the crew orchestrator's prompt.

    Two things it cannot work out on its own:

    1. **Match task weight to worker weight.** Left to itself an orchestrator
       sends everything to one model. Small/fast workers exist for the many
       small tasks a crew generates.
    2. **Do not spend your own quota twice.** A worker on the orchestrator's own
       model or provider draws down the same subscription or rate limit that is
       already keeping the orchestrator alive — so the crew throttles itself
       exactly when it is busiest, and gains no diversity of opinion for it.
    """
    declared = declared if declared is not None else load_declared()
    policy = load_policy(declared)
    lines = [
        "Dispatch rules:",
        "- Match the worker to the task. Send routine edits, lookups, formatting and "
        "summaries to a small/fast worker; reserve large or frontier workers for hard "
        "reasoning, architecture and debugging. Sending everything to the biggest worker "
        "is slow and expensive.",
        "- Prefer a worker whose listed strengths match the task (e.g. a 'code' worker "
        "for implementation, a 'reasoning' worker for analysis).",
        "- Where cost is listed, treat it as real money and prefer the cheapest worker "
        "that can do the job. Where cost is unknown, use the size tier as a proxy.",
        "- Parallelise across DIFFERENT workers rather than queueing on one.",
        "- Prefer a worker whose context window fits the task; do not send a large "
        "codebase to a short-context worker.",
        "- Avoid any worker marked DEPRECATED unless nothing else fits — it can be "
        "withdrawn mid-run.",
    ]
    lines.extend(_policy_rules(worker_models, declared, policy))

    # Non-hallucination rate spreads far wider across a roster than raw
    # capability does, and it is the one axis where the best agentic worker can
    # be the worst choice. Worth calling out separately from the roster lines.
    rates = {
        m: declared[m]["benchmarks"]["non-hallucination-rate"]
        for m in worker_models
        if isinstance(declared.get(m), dict)
        and isinstance(declared[m].get("benchmarks"), dict)
        and "non-hallucination-rate" in declared[m]["benchmarks"]
    }
    # Needs at least two distinct scores, or "lowest X, highest X" names the
    # same worker twice and reads as noise.
    if len(set(rates.values())) >= 2:
        ranked = sorted(rates.items(), key=lambda kv: kv[1])
        worst = ranked[:2]
        best = ranked[-1]
        lines.append(
            "- Check non-hallucination-rate before trusting a factual claim. Lowest here: "
            + ", ".join(f"{m} ({v:g}%)" for m, v in worst)
            + f"; highest: {best[0]} ({best[1]:g}%). A worker can top the agentic boards and "
            "still invent facts, so route research, knowledge and citation work to a "
            "high-rate worker and verify anything a low-rate worker asserts."
        )
    if orchestrator_model:
        fam = family_of(orchestrator_model)
        same_family = sorted({m for m in worker_models if family_of(m) == fam})
        lines.append(
            f"- You are running on {orchestrator_model}. Do NOT delegate to a worker running "
            "that same model: it costs the same quota twice and returns the same judgement."
        )
        if same_family:
            lines.append(
                f"- These workers share your provider ({fam}): {', '.join(same_family)}. "
                "They draw on the same subscription and rate limit you do, so using them "
                "makes you hit limits sooner. Prefer workers from other providers unless a "
                "task specifically needs one of these."
            )
    lines.append(
        "- If you are yourself running on a subscription harness (Claude Code, Codex), "
        "prefer API-model workers for bulk work and keep your own budget for orchestration "
        "and final synthesis."
    )
    return "\n".join(lines)
