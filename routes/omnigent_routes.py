"""Omnigent integration routes for Odysseus."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import threading
from io import BytesIO
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request, Response

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src.auth_helpers import get_current_user, owner_filter, require_authenticated_request
from src.constants import DATA_DIR
from src.omnigent_catalog import load_declared, select_workers
from src.omnigent_native import NativeOmnigentManager
from src.omnigent_manager import INSTALL_GUIDANCE, OmnigentManager

logger = logging.getLogger(__name__)


# Prefer a reasoning-capable general model as the default when installing API
# gateways into the bundled Omnigent.
_DEFAULT_MODEL_PREFS = ("glm-5", "glm", "qwen3", "deepseek", "llama")

# Upper bound on generated crew workers — large enough to install every model a
# typical gateway exposes (W&B lists ~29), bounded to guard against a runaway
# endpoint flooding the picker and the orchestrator's spawn roster.
_MAX_WORKERS = 40

_LEGACY_AGENT_REPOINTS = "generated_agent_repoints.json"
_KNOWN_STALE_SESSION_AGENT_NAMES = {
    "deepseek-v3-1",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm",
    "glm-5-2",
    "openai-agents",
}


def _endpoint_model_ids(ep) -> list[str]:
    ids: list[str] = []
    for raw in (ep.pinned_models, ep.cached_models):
        try:
            for m in json.loads(raw or "[]"):
                mid = m.get("id") if isinstance(m, dict) else m
                if mid and mid not in ids:
                    ids.append(mid)
        except Exception:
            pass
    return ids


def _provider_slug(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in (name or "api").lower()).strip("-")
    return slug or "api"


def _pick_default_model(ids: list[str]) -> str | None:
    for pref in _DEFAULT_MODEL_PREFS:
        for mid in ids:
            if pref in mid.lower():
                return mid
    return ids[0] if ids else None


def _model_slug(model_id: str) -> str:
    tail = (model_id or "").split("/")[-1].lower()
    slug = "".join(c if c.isalnum() else "-" for c in tail).strip("-")
    return slug or "model"


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _executor_block(model_id: str | None, creds: tuple[str, str] | None) -> dict:
    """Build a worker/orchestrator ``executor`` block.

    The model id MUST sit at ``executor.model`` — Omnigent ignores
    ``executor.config.model`` and silently falls back to a catalog default
    (``gpt-5.5``), which the W&B gateway 404s.

    Credentials are baked inline as ``executor.auth: {type: api_key, …}`` rather
    than relying on the default ``providers:`` gateway in ``config.yaml``. The
    provider path resolves the key from the HOME-relative config, but the
    detached Omnigent daemon does NOT carry ``HOME=…/omnigent-home`` (it only
    gets explicit ``--database-uri``/``--artifact-location`` args), so its
    in-process ``load_config()`` reads an empty config and the worker dies with
    "no OpenAI credentials" (root-caused 2026-06-29). ``executor.auth`` is read
    from the spec file by absolute path, so it threads regardless of HOME.

    ``use_responses`` forces the Chat Completions wire (not the OpenAI Responses
    API, which the chat-only W&B gateway 404s). The old ``providers:`` gateway
    set this via ``wire_api: chat``; with inline auth that path is skipped, so
    the spec must declare it. Omnigent's spec parser ``str()``-coerces every
    ``executor.config`` scalar, so ``false``/``False`` both become the *truthy*
    string ``"False"`` and the spawn builder (``"true" if v else "false"``)
    would emit ``"true"``. The empty string is the only value that coerces to a
    falsy string, yielding ``HARNESS_OPENAI_AGENTS_USE_RESPONSES=false``
    (verified 2026-06-29).
    """
    executor: dict = {
        "type": "omnigent",
        "model": model_id,
        "config": {"harness": "openai-agents", "use_responses": ""},
    }
    if creds and creds[1]:
        base_url, api_key = creds
        auth: dict = {"type": "api_key", "api_key": api_key}
        if base_url:
            auth["base_url"] = base_url.rstrip("/")
        executor["auth"] = auth
    return executor


def _os_env_block() -> dict:
    """Caller-process shell/file environment.

    Without an ``os_env`` block an agent gets NO ``sys_os_*`` tools — it's a
    bare chat LLM that can't read the repo, run commands, or edit files, so it
    just narrates and stops. Mirrors polly's workers (``sandbox: none`` — these
    run on the user's own machine to inspect/build the app)."""
    return {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}


def _blast_radius_guardrail() -> dict:
    """Deny the catastrophic command set (force-push, ``rm -rf /``, hard-reset to
    a remote ref) while allowing normal pushes — the runner-side safety net polly
    uses, important here since agents run unsandboxed on the user's machine."""
    return {
        "type": "function",
        "function": {
            "path": "omnigent.inner.nessie.policies.blast_radius",
            "arguments": {"gate_pushes": False},
        },
    }


# Orchestrator brain. polly pins NO model on a claude-sdk orchestrator and lets
# the Claude provider resolve its default — so the orchestrator works under the
# Claude SDK brain (a pinned W&B model id like ``zai-org/GLM-5.2`` makes Claude
# error "model may not exist"). Claude is also far more reliable at the
# tool-calling/delegation an orchestrator needs than a gateway model over the
# chat wire. The W&B models stay as the workers it delegates to.
_CREW_PROMPT = (
    "You are the crew orchestrator — a tech lead, not the doer. You delegate the actual work to a team "
    "of sub-agents. TWO are full coding CLIs — `claude-code` and `codex` — best for writing/editing "
    "code, running tests, and deep investigation. The rest are API models (one per model, via the W&B "
    "gateway), best for research, analysis, and running many opinions in parallel. Your roster: "
    "{workers}.\n\n"
    "ROUTING: send code/implementation and anything that needs tests or careful editing to "
    "`claude-code` or `codex`; use the API models for research, analysis, breadth, and parallel "
    "fan-out. If a worker errors, stalls, or returns nothing useful, RE-DISPATCH that task to a "
    "different worker — never report failure without first retrying on another worker.\n\n"
    "CRITICAL — ACT, DON'T NARRATE. Never end a turn after only saying what you are about to do. If a "
    "sentence describes a next action (reading a file, dispatching a worker), the tool calls that "
    "perform it MUST be in the SAME turn, right after the text. A turn whose entire content is an "
    "intent sentence with no tool call is a dropped turn that stalls the whole run.\n\n"
    "On your FIRST turn: briefly orient yourself with your own tools (sys_os_shell — e.g. `ls`, "
    "`find`, read a key file or two) to locate the code and understand the task, THEN start delegating "
    "in that same turn. A quick scoping look is fine; a sprawling read is not — that's what workers "
    "are for.\n\n"
    "Delegate each scoped task to the best-fit worker via sys_session_send. ALWAYS prefix the `title` "
    "with the chosen worker's model in square brackets so the model is visible in the UI — e.g. "
    "`[deepseek-v4-flash] analyze study models`, `[glm-5-2] research spaced repetition` — and pass a "
    "precise task prompt stating exactly what to produce. Workers run autonomously and have their own shell/file tools; "
    "they notify you via your inbox when done. Collect results with sys_read_inbox — never busy-poll. "
    "Once you've dispatched and have nothing to do but wait, end the turn; you are auto-woken when a "
    "worker finishes. Then synthesize their results into the final deliverable yourself (you may write "
    "docs/plans directly with your own tools)."
)

_WORKER_PROMPT = (
    "You are {slug}, an API worker running on {mid}, dispatched by the crew orchestrator for ONE "
    "scoped task. Begin EVERY reply with a single header line — `▸ model: {mid}` — so the user "
    "always sees which model is answering. "
    "You have shell and file tools (sys_os_*) — USE them to actually DO the task (read "
    "code, run commands, write/edit files); do NOT just describe what you would do. Act in the same "
    "turn — never end a turn after only narrating intent. Stay strictly within your scoped task, then "
    "return a concise, structured result (what you found or changed, with file:line evidence where "
    "relevant). If you hit a wall, say so plainly instead of guessing."
)

# Native-CLI coding sub-agents alongside the W&B API workers. These use the
# user's own logged-in CLIs (no gateway key) and are the robust choice for
# real code work. Claude Code authenticates from the Claude login the crew
# brain already uses; Codex needs a one-time `codex login`. ``permission_mode:
# auto`` / ``yolo: true`` let the headless workers act without approval prompts
# (mirrors polly's claude_code / codex specs).
_CLI_WORKERS = (
    {"slug": "claude-code", "label": "Claude Code", "harness": "claude-native",
     "config": {"permission_mode": "auto"}},
    {"slug": "codex", "label": "Codex", "harness": "codex-native",
     "config": {"yolo": True}},
)


def _cli_worker_spec(w: dict) -> dict:
    return {
        "spec_version": 1,
        "name": w["slug"],
        "description": f"{w['label']} coding sub-agent ({w['harness']} harness, your CLI login).",
        "executor": {"type": "omnigent", "config": {"harness": w["harness"], **w["config"]}},
        "os_env": _os_env_block(),
        "prompt": (
            f"You are {w['label']}, a coding sub-agent dispatched by the crew for ONE scoped task. "
            f"Begin every reply with a single header line — `▸ {w['label']}`. Your task says "
            "IMPLEMENT, REVIEW, or EXPLORE — do exactly that, strictly in scope, USING your tools to "
            "actually do it (don't just describe). Return a concise, structured result with file:line "
            "evidence; note anything that didn't fit the task."
        ),
        "guardrails": {"policies": {"blast_radius": _blast_radius_guardrail()}},
    }


def _api_worker_spec(slug: str, mid: str, creds: tuple[str, str] | None, endpoint_name: str = "API") -> dict:
    return {
        "spec_version": 1,
        "name": slug,
        "description": f"{mid} worker (via {endpoint_name}).",
        "executor": _executor_block(mid, creds),
        "os_env": _os_env_block(),
        "prompt": _WORKER_PROMPT.format(slug=slug, mid=mid) + _TOOL_PROTOCOL,
        "guardrails": {"policies": {"blast_radius": _blast_radius_guardrail()}},
    }


def _write_worker(agents_dir: Path, slug: str, spec: dict) -> None:
    wdir = agents_dir / slug
    wdir.mkdir(parents=True, exist_ok=True)
    cfg_file = wdir / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    _chmod_600(cfg_file)


def _write_crew_file(crew_dir: Path, spec: dict) -> None:
    crew_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = crew_dir / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    _chmod_600(cfg_file)


# Mechanical tool-use protocol appended to every generated agent prompt. Weak
# tool-calling models (gateway models over the chat wire, e.g. GLM) tend to
# NARRATE tool use ("I'll dispatch codex to run the grid search") and end the
# turn without emitting any function call — nothing happens. Abstract "act,
# don't narrate" phrasing is not enough for them; this block is deliberately
# blunt, example-driven, and sits at the END of the prompt where models attend
# most.
_TOOL_PROTOCOL = (
    "\n\n=== TOOL-CALL PROTOCOL (MANDATORY) ===\n"
    "Work happens ONLY through function calls. Text does nothing. Writing about an action does not "
    "perform it.\n"
    "WRONG (this is a failure): \"I'll dispatch a codex sub-agent to run the grid search while I run "
    "rsi_sd myself.\" — a reply that only announces actions, with no function call attached.\n"
    "RIGHT: emit the sys_session_send function call (and any sys_os_shell calls) IN THIS REPLY, then "
    "briefly say what you started.\n"
    "Rules:\n"
    "1. If your reply says you will do, run, dispatch, check, read, or launch ANYTHING, the same reply "
    "MUST contain the function call(s) that do it. No exceptions.\n"
    "2. Never describe a shell command, file read, or delegation in prose instead of calling the tool.\n"
    "3. Only two valid ways to end a reply: (a) function calls are attached, or (b) the task is DONE "
    "and you are giving the final answer with results you actually obtained through earlier calls.\n"
    "4. Before finishing, re-read your reply: if it contains a future-tense promise (\"I'll...\", "
    "\"Let me...\", \"Next I will...\") with no attached function call, DELETE the promise and emit "
    "the call instead."
)


def _crew_config(
    name: str,
    description: str,
    executor: dict,
    slugs: list[str],
    primary_worker: str | None = None,
    lead_model: str | None = None,
    prompt: str | None = None,
) -> dict:
    worker_list = ", ".join(slugs) if slugs else "none"
    if prompt is not None:
        pass
    elif lead_model:
        prompt = (
            f"You are `{name}`, the crew lead running directly on `{lead_model}`. "
            "That selected model is your own executor; do not delegate the first task just to prove "
            "the model is involved. Use your own sys_os_* tools to inspect files, run commands, and "
            "produce the answer directly.\n\n"
            f"Optional sub-agents: {worker_list}. Delegate with sys_session_send only when a worker "
            "is clearly better suited, when you want parallel review, or when you need a fallback. "
            "Codex is the coding/test-heavy fallback; you remain responsible for the final answer.\n\n"
            "CRITICAL - ACT, DON'T NARRATE. Never end a turn after only saying what you are about to "
            "do. If a sentence describes a next action, the tool calls that perform it MUST be in the "
            "same turn. On your first turn, use your own tools to orient and start the actual work."
            + _TOOL_PROTOCOL
        )
    else:
        prompt = _CREW_PROMPT.format(workers=worker_list) + _TOOL_PROTOCOL
    if primary_worker and not lead_model and prompt is None:
        prompt = (
            f"This crew is anchored on `{primary_worker}`. On the first turn, immediately delegate "
            f"the user's substantive task to `{primary_worker}` with sys_session_send. Do not emit "
            "a progress-only message before that tool call. After workers finish, call sys_read_inbox "
            "and synthesize the results. If the anchored worker fails or stalls, re-dispatch the same "
            "scoped task to another configured worker.\n\n"
            f"{prompt}"
        )
    return {
        "spec_version": 1,
        "name": name,
        "description": description,
        "executor": executor,
        "spawn": True,
        "async": True,
        "cancellable": True,
        "timers": True,
        "os_env": _os_env_block(),
        "terminals": {
            "shell": {"command": "bash", "allow_cwd_override": True, "os_env": _os_env_block()},
        },
        "prompt": prompt,
        "tools": {"agents": slugs},
        "guardrails": {
            "ask_timeout": 86400,
            "policies": {
                "spawn_bounds": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.inner.nessie.policies.spawn_bounds",
                        "arguments": {
                            "max_dispatches_per_turn": 5,
                            "dispatch_tools": ["sys_session_send", "sys_session_create"],
                        },
                    },
                },
                "blast_radius": _blast_radius_guardrail(),
            },
        },
    }


# ---------------------------------------------------------------------------
# The universal crew
# ---------------------------------------------------------------------------
#
# One crew, `crew`, replaces the old per-model variants (crew-claude,
# crew-codex, crew-<model>). Its orchestrator is whatever model the user picks
# in the Omnigent panel, and every other connected model is on its roster as a
# sub-agent, described with what it costs and whether it is the newest of its
# family, so the orchestrator can choose the right worker for each task.

_CREW_NAME = "crew"
_CREW_SETTINGS_FILE = "omnigent-crew.json"
_GENERATED_MANIFEST = ".odysseus-generated.json"
_DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
# Descriptions the previous generator wrote; a crew-* dir carrying one of them
# is ours to retire even before a manifest existed.
_LEGACY_GENERATED_MARKERS = (
    "-brained crew", "Claude-brained", "Codex-brained", "API-model workers",
)
_NATIVE_WORKER_LINES = {
    "claude-code": (
        "Claude Code CLI, a full coding agent on the Claude subscription logged in inside "
        "Omnigent (`claude login`). Best for implementation, refactors, debugging and tests. "
        "Flat-rate: uses plan limits, no per-token charge."
    ),
    "codex": (
        "Codex CLI, a full coding agent on the ChatGPT subscription logged in inside Omnigent "
        "(`codex login`). Best for implementation and test-heavy work. Flat-rate: uses plan "
        "limits, no per-token charge."
    ),
}


def _crew_settings_path() -> Path:
    return Path(DATA_DIR) / _CREW_SETTINGS_FILE


def load_crew_settings() -> dict:
    """The user's crew choices, with defaults for anything unset."""
    settings = {"orchestrator": "claude", "reasoning_effort": "high", "max_workers": _MAX_WORKERS}
    try:
        raw = json.loads(_crew_settings_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            settings.update({k: v for k, v in raw.items() if k in settings})
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("omnigent crew settings unreadable error_type=%s", type(exc).__name__)
    if settings["reasoning_effort"] not in _REASONING_EFFORTS:
        settings["reasoning_effort"] = "high"
    try:
        settings["max_workers"] = max(1, min(_MAX_WORKERS, int(settings["max_workers"])))
    except (TypeError, ValueError):
        settings["max_workers"] = _MAX_WORKERS
    return settings


def save_crew_settings(settings: dict) -> None:
    path = _crew_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")
    _chmod_600(path)


def _launched_crew_path() -> Path:
    return _crew_settings_path().with_name("omnigent-crew-launched.json")


def mark_crew_launched(settings: dict) -> None:
    """Remember which crew settings the running server was started with."""
    path = _launched_crew_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")


def crew_pending_restart(running: bool) -> bool:
    """True when the running server's crew is older than the saved choice."""
    if not running:
        return False
    try:
        launched = json.loads(_launched_crew_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(launched, dict) and launched != load_crew_settings()


def parse_orchestrator_id(value: str | None) -> dict:
    """``claude`` / ``claude::<model>`` / ``codex[::<model>]`` / ``api::<endpoint>::<model>``."""
    value = (value or "claude").strip()
    kind, _, rest = value.partition("::")
    if kind in ("claude", "codex"):
        return {"kind": kind, "model": rest or None, "endpoint_id": None}
    if kind == "api" and "::" in rest:
        endpoint_id, _, model = rest.partition("::")
        if endpoint_id and model:
            return {"kind": "api", "model": model, "endpoint_id": endpoint_id}
    return {"kind": "claude", "model": None, "endpoint_id": None}


def _manifest_path(agents_root: Path) -> Path:
    return agents_root.parent / _GENERATED_MANIFEST


def _read_manifest(agents_root: Path) -> dict:
    try:
        data = json.loads(_manifest_path(agents_root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_manifest(agents_root: Path, data: dict) -> None:
    path = _manifest_path(agents_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    _chmod_600(path)


def _looks_generated(crew_dir: Path) -> bool:
    if crew_dir.name in ("crew-claude", "crew-codex") or crew_dir.name.startswith("crew-api-"):
        return True
    try:
        cfg = yaml.safe_load((crew_dir / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    desc = str(cfg.get("description") or "") if isinstance(cfg, dict) else ""
    return any(marker in desc for marker in _LEGACY_GENERATED_MARKERS)


def _retire_generated_crews(agents_root: Path, manifest: dict) -> list[str]:
    """Remove crews an earlier Odysseus generated; never a crew the user made."""
    retired: list[str] = []
    candidates = {name for name in manifest.get("agents") or [] if isinstance(name, str)}
    candidates.update(p.name for p in agents_root.glob("crew-*") if p.is_dir())
    for name in sorted(candidates):
        if name == _CREW_NAME or "/" in name or name.startswith("."):
            continue
        crew_dir = agents_root / name
        if not crew_dir.is_dir():
            continue
        if name in (manifest.get("agents") or []) or _looks_generated(crew_dir):
            shutil.rmtree(crew_dir, ignore_errors=True)
            retired.append(name)
    shutil.rmtree(agents_root / _CREW_NAME, ignore_errors=True)
    return retired


def _rank_worker_models(ids: list[str], entries: dict, limit: int) -> list[str]:
    """Recommended models first, then the catalog's measured/scale ranking."""
    try:
        declared = load_declared(path=str(Path(DATA_DIR) / "omnigent-model-costs.json"))
        ranked = select_workers(ids, declared, limit=len(ids) or 1)
    except Exception:
        ranked = list(ids)
    order = {mid: i for i, mid in enumerate(ranked)}
    kept = [mid for mid in ids if mid in order]
    kept.sort(key=lambda mid: (not getattr(entries.get(mid), "recommended", False), order[mid]))
    return kept[:limit]


def _roster_entries(model_creds: dict, model_meta: dict | None) -> dict:
    """model id -> RosterEntry (recommended, tier, cost) for the crew prompt."""
    from src import model_roster

    rows = []
    for mid in model_creds:
        meta = (model_meta or {}).get(mid) or {}
        rows.append((meta.get("endpoint_id") or "api", meta.get("endpoint_name") or "API",
                     mid, meta.get("kind") or "api", meta.get("provider") or "openai"))
    try:
        return {e.model: e for e in model_roster.build_entries(rows)}
    except Exception as exc:
        logger.warning("omnigent roster metadata failed error_type=%s", type(exc).__name__)
        return {}


def _worker_line(slug: str, mid: str, entry, endpoint_name: str) -> str:
    if entry is None:
        return f"- `{slug}`: {mid} via {endpoint_name}"
    bits = []
    if entry.recommended:
        bits.append("RECOMMENDED")
    bits.append(f"{entry.kind}, {entry.tier}")
    if entry.traits:
        bits.append("good at " + "/".join(entry.traits))
    if entry.context_k:
        bits.append(f"{entry.context_k}k context")
    bits.append(entry.cost_label())
    if entry.notes:
        bits.append(str(entry.notes)[:120])
    return f"- `{slug}`: {mid} via {endpoint_name} — " + "; ".join(bits)


def _orchestrator_executor(orch: dict, model_creds: dict, effort: str) -> tuple[dict, dict | None, str, str | None]:
    """(executor, llm block, human label, own model id) for the chosen orchestrator."""
    if orch["kind"] == "api" and orch.get("model") in model_creds:
        mid = orch["model"]
        return _executor_block(mid, model_creds[mid]), None, f"{mid} (API)", mid
    if orch["kind"] == "codex":
        model = orch.get("model") or _DEFAULT_CODEX_MODEL
        executor = {
            "type": "omnigent",
            "model": model,
            "context_window": 1000000,
            "config": {"harness": "codex-native", "yolo": True, "reasoning_effort": effort},
        }
        return executor, {"model": model, "reasoning_effort": effort}, f"Codex ({model})", None
    # Claude Code login inside Omnigent (claude-sdk). With no model pinned the
    # Claude provider picks its default; a pinned Claude id is passed through.
    executor = {"type": "omnigent", "context_window": 1000000, "config": {"harness": "claude-sdk"}}
    model = orch.get("model") if orch["kind"] == "claude" else None
    if model:
        executor["model"] = model
    return executor, None, f"Claude ({model or 'default model'})", None


def _universal_prompt(label: str, own_model: str | None, lines: list[str], worker_models: list[str]) -> str:
    from src.model_roster import ROUTING_GUIDANCE

    try:
        declared = load_declared(path=str(Path(DATA_DIR) / "omnigent-model-costs.json"))
        from src.omnigent_catalog import dispatch_guidance

        guidance = dispatch_guidance(own_model, worker_models, declared)
    except Exception:
        guidance = ""
    roster = "\n".join(lines) if lines else "- (no sub-agents connected: do the work yourself)"
    return (
        f"You are the crew orchestrator, running on {label}. You are a tech lead, not the doer: break "
        "the goal into scoped tasks, give each to the best-suited sub-agent, then synthesize their "
        "results. Every model the user has connected is on your roster; each sub-agent has its own "
        "shell and file tools.\n\n"
        "Your roster (RECOMMENDED = newest model of its family; cost is per 1M tokens):\n"
        f"{roster}\n\n"
        f"{ROUTING_GUIDANCE}\n\n"
        f"{guidance}\n\n"
        "If a worker errors, stalls, or returns nothing useful, RE-DISPATCH that task to a different "
        "worker; never report failure without first retrying elsewhere.\n\n"
        "Delegate each scoped task via sys_session_send. ALWAYS prefix the `title` with the worker's "
        "model in square brackets, e.g. `[deepseek-v4-flash] summarise the logs`, and give a precise "
        "task prompt stating exactly what to produce. Workers notify your inbox when done; collect "
        "results with sys_read_inbox, never busy-poll. Once you have dispatched and have nothing to do "
        "but wait, end the turn; you are woken when a worker finishes.\n\n"
        "On your FIRST turn, orient briefly with your own tools (sys_os_shell: `ls`, read a key file) "
        "and start delegating in that same turn."
        + _TOOL_PROTOCOL
    )


def _generate_crew(
    model_creds: dict[str, tuple[str, str]],
    orchestrator: str | None = None,
    *,
    model_meta: dict | None = None,
    settings: dict | None = None,
) -> int:
    """Write the universal crew; returns how many sub-agents it can delegate to.

    ``model_creds`` maps each API/local model id to ``(base_url, api_key)``;
    ``model_meta`` optionally maps it to ``{endpoint_id, endpoint_name, kind,
    provider}`` for the roster. ``orchestrator`` overrides the saved setting.
    """
    settings = settings or load_crew_settings()
    orch = parse_orchestrator_id(orchestrator or settings.get("orchestrator"))
    effort = settings.get("reasoning_effort") or "high"
    agents_root = Path(DATA_DIR) / "omnigent-home" / ".omnigent" / "agents"
    agents_root.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(agents_root)
    retired = _retire_generated_crews(agents_root, manifest)

    entries = _roster_entries(model_creds, model_meta)
    executor, llm_block, label, own_model = _orchestrator_executor(orch, model_creds, effort)
    worker_ids = _rank_worker_models([m for m in model_creds if m and m != own_model],
                                     entries, int(settings.get("max_workers") or _MAX_WORKERS))

    crew_dir = agents_root / _CREW_NAME
    slugs: list[str] = []
    lines: list[str] = []
    used: set[str] = {w["slug"] for w in _CLI_WORKERS} | {_CREW_NAME}
    for w in _CLI_WORKERS:
        _write_worker(crew_dir / "agents", w["slug"], _cli_worker_spec(w))
        slugs.append(w["slug"])
        lines.append(f"- `{w['slug']}`: {_NATIVE_WORKER_LINES.get(w['slug'], w['label'])}")
    for mid in worker_ids:
        slug = _model_slug(mid)
        while slug in used:
            slug += "-x"
        used.add(slug)
        endpoint_name = ((model_meta or {}).get(mid) or {}).get("endpoint_name") or "API"
        _write_worker(crew_dir / "agents", slug, _api_worker_spec(slug, mid, model_creds.get(mid), endpoint_name))
        slugs.append(slug)
        lines.append(_worker_line(slug, mid, entries.get(mid), endpoint_name))

    spec = _crew_config(
        _CREW_NAME,
        f"Universal crew: {label} orchestrates every connected model (Claude Code, Codex and "
        f"{len(worker_ids)} API/local models).",
        executor,
        slugs,
        prompt=_universal_prompt(label, own_model, lines, worker_ids),
    )
    if llm_block:
        spec["llm"] = llm_block
    _write_crew_file(crew_dir, spec)
    _write_manifest(agents_root, {
        "agents": [_CREW_NAME],
        "retired": sorted(set(manifest.get("retired") or []) | set(retired)),
        "providers": manifest.get("providers") or [],
        "orchestrator": orch,
    })
    return len(slugs)


def _generated_crew_dirs(agents_root: Path) -> list[Path]:
    crew = agents_root / _CREW_NAME
    return [crew] if (crew / "config.yaml").exists() else []


def _retired_agent_names(agents_root: Path) -> set[str]:
    return {n for n in (_read_manifest(agents_root).get("retired") or []) if isinstance(n, str)}


def _generated_api_worker_slugs(crew_dirs: list[Path]) -> set[str]:
    cli_worker_slugs = {str(w["slug"]) for w in _CLI_WORKERS}
    slugs: set[str] = set()
    for crew in crew_dirs:
        worker_root = crew / "agents"
        if not worker_root.exists():
            continue
        for worker in worker_root.iterdir():
            if (worker / "config.yaml").exists() and worker.name not in cli_worker_slugs:
                slugs.add(worker.name)
    return slugs


def _artifact_model_slug(root: Path, bundle_location: str | None) -> str | None:
    if not bundle_location:
        return None
    bundle_path = root / "artifacts" / bundle_location
    try:
        if not bundle_path.is_file():
            return None
        with tarfile.open(bundle_path, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            member = next((m for m in members if Path(m.name).name == "config.yaml"), None)
            if member is None:
                member = next((m for m in members if Path(m.name).suffix in {".yaml", ".yml"}), None)
            if member is None:
                return None
            f = tf.extractfile(member)
            if f is None:
                return None
            cfg = yaml.safe_load(f.read(262_144)) or {}
    except Exception:
        return None
    executor = cfg.get("executor") if isinstance(cfg, dict) else None
    model = executor.get("model") if isinstance(executor, dict) else None
    return _model_slug(str(model)) if model else None


def _builtin_agent_env() -> dict[str, str] | None:
    """Register generated top-level crews as built-ins for the picker."""
    agents_root = Path(DATA_DIR) / "omnigent-home" / ".omnigent" / "agents"
    dirs = [str(p) for p in _generated_crew_dirs(agents_root)]
    return {"OMNIGENT_BUILTIN_AGENT_DIRS": os.pathsep.join(dirs)} if dirs else None


def _agent_id_to_key(id_val) -> str:
    """Encode an agent id for the repoints file. BLOB ids become hex: prefixed."""
    if isinstance(id_val, (bytes, bytearray, memoryview)):
        return f"hex:{bytes(id_val).hex()}"
    return str(id_val)


def _key_to_agent_id(key: str):
    """Decode a repoints key back to the DB id value (bytes for hex:)."""
    if isinstance(key, str) and key.startswith("hex:"):
        try:
            return bytes.fromhex(key[4:])
        except Exception:
            return key
    return key


def _purge_generated_builtin_agent_rows() -> int:
    root = Path(DATA_DIR) / "omnigent-home" / ".omnigent"
    db_path = root / "chat.db"
    agents_root = root / "agents"
    if not db_path.exists():
        return 0
    crew_dirs = _generated_crew_dirs(agents_root)
    names: set[str] = {p.name for p in crew_dirs}
    names.update(p.name for p in agents_root.glob("crew-api-*") if (p / "config.yaml").exists())
    names.update(_generated_api_worker_slugs([*crew_dirs, *agents_root.glob("crew-api-*")]))
    # Crews an earlier version generated (crew-claude, crew-<model>, ...): their
    # built-in template rows would otherwise linger in Omnigent's picker.
    names.update(_retired_agent_names(agents_root))
    if not names:
        return 0
    placeholders = ",".join("?" for _ in names)
    repoint_path = root / _LEGACY_AGENT_REPOINTS
    with sqlite3.connect(db_path) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(agents)").fetchall()}
        has_kind = "kind" in cols
        has_session = "session_id" in cols
        if has_kind:
            template_where = "kind = 1"
        elif has_session:
            template_where = "session_id IS NULL"
        else:
            return 0
        try:
            legacy = con.execute(
                f"SELECT id, name FROM agents WHERE {template_where} AND name LIKE 'crew-api-%'"
            ).fetchall()
        except sqlite3.OperationalError:
            legacy = []
        repoints = {}
        for row in legacy:
            id_val, name = row[0], row[1]
            if str(name).startswith("crew-api-"):
                repoints[_agent_id_to_key(id_val)] = f"crew-{str(name)[len('crew-api-'):]}"
        if repoints:
            repoint_path.write_text(json.dumps(repoints, sort_keys=True))
            _chmod_600(repoint_path)
        else:
            try:
                repoint_path.unlink()
            except FileNotFoundError:
                pass
        if has_kind:
            cur = con.execute(
                f"DELETE FROM agents WHERE {template_where} "
                f"AND (name IN ({placeholders}) OR name LIKE 'crew-api-%')",
                tuple(sorted(names)),
            )
        else:
            cur = con.execute(
                "DELETE FROM agents WHERE session_id IS NULL "
                f"AND (name IN ({placeholders}) OR name LIKE 'crew-api-%')",
                tuple(sorted(names)),
            )
        con.commit()
        return int(cur.rowcount or 0)


def _refresh_generated_session_agent_rows() -> int:
    root = Path(DATA_DIR) / "omnigent-home" / ".omnigent"
    db_path = root / "chat.db"
    if not db_path.exists():
        return 0
    crew_dirs = _generated_crew_dirs(root / "agents")
    crew_names = {p.name for p in crew_dirs}
    if not crew_names:
        return 0
    api_worker_slugs = _generated_api_worker_slugs(crew_dirs)
    retired_names = _retired_agent_names(root / "agents")
    placeholders = ",".join("?" for _ in crew_names)
    repoint_path = root / _LEGACY_AGENT_REPOINTS
    total = 0
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        cols = {row[1] for row in con.execute("PRAGMA table_info(agents)").fetchall()}
        has_kind = "kind" in cols
        has_session = "session_id" in cols
        if has_kind:
            template_where = "kind = 1"
            session_where = "kind = 2"
        elif has_session:
            template_where = "session_id IS NULL"
            session_where = "session_id IS NOT NULL"
        else:
            return 0
        fresh_rows = con.execute(
            f"SELECT id, name, bundle_location, version FROM agents "
            f"WHERE {template_where} AND name IN ({placeholders})",
            tuple(sorted(crew_names)),
        ).fetchall()
        fresh = {str(row["name"]): row for row in fresh_rows}
        for name, row in fresh.items():
            try:
                session_id_rows = con.execute(
                    f"SELECT id FROM agents WHERE {session_where} AND name = ?",
                    (name,),
                ).fetchall()
            except sqlite3.OperationalError:
                session_id_rows = []
            session_ids = [r["id"] for r in session_id_rows]
            try:
                cur = con.execute(
                    f"UPDATE agents SET bundle_location = ?, version = ? "
                    f"WHERE {session_where} AND name = ?",
                    (row["bundle_location"], row["version"], name),
                )
                total += int(cur.rowcount or 0)
            except sqlite3.OperationalError:
                pass
            if session_ids:
                session_placeholders = ",".join("?" for _ in session_ids)
                # Need to handle both TEXT and BLOB agent_id types; use actual values
                try:
                    cur = con.execute(
                        "UPDATE conversations SET agent_id = ? "
                        f"WHERE agent_id IN ({session_placeholders})",
                        tuple([row["id"]] + session_ids),
                    )
                    total += int(cur.rowcount or 0)
                except sqlite3.OperationalError:
                    pass
        repoints: dict[str, str] = {}
        if repoint_path.exists():
            try:
                repoints = json.loads(repoint_path.read_text()) or {}
            except Exception:
                repoints = {}
        for old_id_key, new_name in repoints.items():
            # A legacy crew-api-* chat points at its per-model crew; once that
            # crew is retired it continues on the universal crew instead.
            row = fresh.get(str(new_name)) or fresh.get(_CREW_NAME)
            if not row:
                continue
            old_id_val = _key_to_agent_id(str(old_id_key))
            try:
                cur = con.execute(
                    "UPDATE conversations SET agent_id = ? WHERE agent_id = ?",
                    (row["id"], old_id_val),
                )
                total += int(cur.rowcount or 0)
            except sqlite3.OperationalError:
                # fallback for TEXT id stored as string
                try:
                    cur = con.execute(
                        "UPDATE conversations SET agent_id = ? WHERE agent_id = ?",
                        (row["id"], str(old_id_key)),
                    )
                    total += int(cur.rowcount or 0)
                except Exception:
                    pass
        fallback_row = fresh.get("crew-codex") or fresh.get("crew")
        try:
            stale_rows = con.execute(
                f"SELECT id, name, bundle_location FROM agents WHERE {session_where}"
            ).fetchall()
        except sqlite3.OperationalError:
            stale_rows = []
        for stale in stale_rows:
            name = str(stale["name"])
            if name in fresh:
                continue
            target_names: list[str] = []
            generatedish = False
            if name.startswith("crew-api-"):
                target_names.append(f"crew-{name[len('crew-api-'):]}")
                generatedish = True
            if name in api_worker_slugs:
                target_names.append(f"crew-{name}")
                generatedish = True
            if name in _KNOWN_STALE_SESSION_AGENT_NAMES:
                target_names.append(f"crew-{name}")
                generatedish = True
            if name in retired_names:
                # A conversation on a retired per-model crew continues on the
                # universal crew rather than losing its agent.
                generatedish = True
            if generatedish:
                model_slug = _artifact_model_slug(root, stale["bundle_location"])
                if model_slug:
                    target_names.append(f"crew-{model_slug}")
                if fallback_row:
                    target_names.append(str(fallback_row["name"]))
            target_row = next((fresh[target] for target in target_names if target in fresh), None)
            if not target_row:
                continue
            try:
                cur = con.execute(
                    "UPDATE conversations SET agent_id = ? WHERE agent_id = ?",
                    (target_row["id"], stale["id"]),
                )
                total += int(cur.rowcount or 0)
                cur = con.execute("DELETE FROM agents WHERE id = ?", (stale["id"],))
                total += int(cur.rowcount or 0)
            except sqlite3.OperationalError:
                pass
        con.commit()
    try:
        repoint_path.unlink()
    except FileNotFoundError:
        pass
    return total


def _credentials_env_for(base_url: str | None, api_key: str | None) -> dict[str, str] | None:
    """Build the ambient OpenAI-compatible env a *spawned* openai-agents worker
    needs to authenticate.

    When the crew orchestrator spawns a worker, that worker's executor resolves
    its key as: ``executor.auth`` (none) -> ambient ``OPENAI_BASE_URL`` ->
    ambient ``OPENAI_API_KEY`` -> else "no OpenAI credentials were found". The
    ``config.yaml`` gateway provider is only consulted for top-level runs, so the
    spawn path needs these in the environment. ``USE_RESPONSES=false`` forces
    Chat Completions — the W&B gateway is chat-only and 404s the Responses API.
    """
    base = (base_url or "").rstrip("/")
    if not (base and api_key):
        return None
    return {
        "OPENAI_BASE_URL": base,
        "OPENAI_API_KEY": api_key,
        "HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL": base,
        "HARNESS_OPENAI_AGENTS_API_KEY": api_key,
        "HARNESS_OPENAI_AGENTS_USE_RESPONSES": "false",
    }


def _gateway_credentials_env(user: str | None) -> dict[str, str] | None:
    """Import the default API endpoint's decrypted key from Odysseus so the
    bundled Omnigent server can hand it to delegated/spawned workers. The key is
    never returned to the caller or echoed in the launch response — it only ever
    enters the server process's environment."""
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if user:
            q = owner_filter(q, ModelEndpoint, user)
        endpoints = [
            ep for ep in q.all()
            if (ep.model_type or "llm") == "llm" and ep.base_url and ep.api_key
        ]
        if not endpoints:
            return None
        # The endpoint that becomes the default `openai` gateway provider (the
        # first with usable models) is the one the spawned workers route through.
        chosen = next((ep for ep in endpoints if _endpoint_model_ids(ep)), endpoints[0])
        return _credentials_env_for(chosen.base_url, chosen.api_key)
    finally:
        db.close()


# Placeholder key for keyless local servers (Ollama, LM Studio, llama.cpp):
# openai-agents refuses to start a worker with no key at all.
_LOCAL_PLACEHOLDER_KEY = "not-needed"


def _crew_endpoints(user: str | None):
    """Endpoints Omnigent can call as OpenAI-compatible gateways, with roster kind.

    Subscription endpoints are left out: Claude and ChatGPT plans reach the
    crew through the native Claude Code / Codex workers, and their Odysseus
    endpoints are not plain OpenAI-compatible URLs.
    """
    from src import model_roster

    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if user:
            q = owner_filter(q, ModelEndpoint, user)
        out = []
        for ep in q.all():
            if (ep.model_type or "llm") != "llm" or not ep.base_url:
                continue
            kind, provider = model_roster.classify_endpoint(ep)
            if kind == "subscription":
                continue
            if not ep.api_key and kind != "local":
                continue
            out.append((ep, kind, provider))
        return out
    finally:
        db.close()


def _install_api_models(user: str | None) -> dict:
    """Write the caller's API/local endpoints into the bundled Omnigent and
    regenerate the universal crew.

    Each endpoint becomes an OpenAI-compatible ``gateway`` provider in
    ``config.yaml`` (0600). Providers the user set up themselves with
    ``omnigent setup`` are kept; only the ones Odysseus wrote last time (listed
    in the generated manifest, or pointing at one of the caller's endpoints)
    are replaced.
    """
    from src.endpoint_resolver import _NON_CHAT_MODEL

    providers: dict[str, dict] = {}
    default_model: str | None = None
    model_count = 0
    model_creds: dict[str, tuple[str, str]] = {}
    model_meta: dict[str, dict] = {}
    our_bases: set[str] = set()
    used: set[str] = set()
    for ep, kind, provider in _crew_endpoints(user):
        slug = "ody-" + _provider_slug(ep.name or ep.base_url)
        while slug in used:
            slug += "-x"
        used.add(slug)
        ids = [m for m in _endpoint_model_ids(ep) if not any(p in m.lower() for p in _NON_CHAT_MODEL)]
        model_count += len(ids)
        ep_base = (ep.base_url or "").rstrip("/")
        our_bases.add(ep_base)
        key = ep.api_key or _LOCAL_PLACEHOLDER_KEY
        for mid in ids:
            if mid not in model_creds:
                model_creds[mid] = (ep_base, key)
                model_meta[mid] = {"endpoint_id": ep.id, "endpoint_name": ep.name or ep_base,
                                   "kind": kind, "provider": provider}
        pick = _pick_default_model(ids)
        if default_model is None and pick:
            default_model = pick
        family = {"base_url": ep_base, "api_key": key, "wire_api": "chat"}
        if pick:
            family["models"] = {"default": pick}
        providers[slug] = {"kind": "gateway", "openai": family}

    cfg_dir = Path(DATA_DIR) / "omnigent-home" / ".omnigent"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    agents_root = cfg_dir / "agents"
    manifest = _read_manifest(agents_root)
    ours_before = set(manifest.get("providers") or [])
    cfg_path = cfg_dir / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    kept: dict[str, dict] = {}
    for name, block in (cfg.get("providers") or {}).items():
        base = ""
        if isinstance(block, dict) and isinstance(block.get("openai"), dict):
            base = str(block["openai"].get("base_url") or "").rstrip("/")
        if name in ours_before or name.startswith("ody-") or (base and base in our_bases):
            continue
        kept[name] = block
    merged = {**kept, **providers}
    if providers and not any(isinstance(b, dict) and b.get("default") for b in kept.values()):
        first = next(iter(providers))
        merged[first] = {**providers[first], "default": ["openai"]}
    cfg["providers"] = merged
    cfg.setdefault("harness", "openai-agents")
    if default_model and not kept:
        cfg["model"] = default_model
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    _chmod_600(cfg_path)

    workers = _generate_crew(model_creds, model_meta=model_meta)
    manifest = _read_manifest(agents_root)
    manifest["providers"] = sorted(providers)
    _write_manifest(agents_root, manifest)
    try:
        purged_builtin_rows = _purge_generated_builtin_agent_rows()
    except Exception:
        purged_builtin_rows = 0
    return {
        "endpoints": len(providers),
        "models": model_count,
        "default_model": default_model,
        "workers": workers,
        "orchestrator": load_crew_settings().get("orchestrator"),
        "purged_builtin_rows": purged_builtin_rows,
    }


def _providers() -> list[dict]:
    return [
        {
            "id": "chatgpt-subscription",
            "label": "ChatGPT Subscription",
            "kind": "account",
            "supported": True,
            "auth_flow": "chatgpt-subscription",
            "description": "Use the ChatGPT account already linked through Odysseus, alongside scoped Odysseus tools.",
        },
        {
            "id": "claude-subscription",
            "label": "Claude Subscription",
            "kind": "cli",
            "supported": True,
            "description": "Use a Claude subscription configured in the local Claude/Omnigent environment.",
            "setup_hint": "Run omnigent setup or your Claude CLI login, then choose the Claude harness in Omnigent.",
        },
        {
            "id": "api-endpoint",
            "label": "API endpoint",
            "kind": "api",
            "supported": True,
            "description": "Use any paid or local API endpoint already configured in Odysseus, including OpenAI-compatible providers.",
        },
        {
            "id": "glm-free-web",
            "label": "GLM website",
            "kind": "web",
            "supported": False,
            "url": "https://chat.z.ai/",
            "note": "GLM free website access is not an API connector. Odysseus already has existing Z.AI API endpoints for API and Coding Plan use, so no paid GLM duplicate is added here.",
        },
    ]


def _bundle_bytes(root: Path) -> bytes:
    if not root.exists():
        raise HTTPException(404, "Omnigent bundle not found")
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(root.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            tf.add(path, arcname=str(path.relative_to(root)).replace("\\", "/"))
    return buf.getvalue()


def _has_visible_model_endpoint(request: Request | None = None) -> bool:
    user = get_current_user(request) if request is not None else None
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if user:
            q = owner_filter(q, ModelEndpoint, user)
        return q.first() is not None
    except Exception:
        return False
    finally:
        db.close()


# Launch, start and orchestrator changes rewrite the same config files and
# restart the same server; one at a time.
_LAUNCH_LOCK = threading.Lock()


def _option(id_: str, group: str, label: str, model: str | None, entry=None) -> dict:
    data = {"id": id_, "group": group, "label": label, "model": model,
            "recommended": False, "tier": None, "cost_label": None, "cost_band": None}
    if entry is not None:
        data.update({"recommended": entry.recommended, "tier": entry.tier,
                     "cost_label": entry.cost_label(), "cost_band": entry.cost_band()})
    return data


def orchestrator_options(user: str | None) -> list[dict]:
    """Every model that can lead the crew, recommended first within each group."""
    from src import claude_subscription, model_roster

    entries = model_roster.roster(user)
    claude_models = [e for e in entries if e.provider == "claude-subscription"]
    chatgpt_models = [e for e in entries if e.provider == "chatgpt-subscription"]
    sub_note = "flat-rate; uses the login inside Omnigent"
    options = [_option("claude", "Claude Code (subscription)", "Claude — default model", None)]
    options[0]["cost_label"] = sub_note
    if claude_models:
        options += [_option(f"claude::{e.model}", "Claude Code (subscription)", f"Claude — {e.model}", e.model, e)
                    for e in claude_models]
    else:
        options += [_option(f"claude::{m}", "Claude Code (subscription)", f"Claude — {m}", m)
                    for m in claude_subscription.default_models()]
    options.append(_option("codex", "Codex (subscription)", f"Codex — {_DEFAULT_CODEX_MODEL}", _DEFAULT_CODEX_MODEL))
    options[-1]["cost_label"] = sub_note
    options += [_option(f"codex::{e.model}", "Codex (subscription)", f"Codex — {e.model}", e.model, e)
                for e in chatgpt_models if e.model != _DEFAULT_CODEX_MODEL]
    usable = {ep.id for ep, _kind, _provider in _crew_endpoints(user)}
    for e in entries:
        if e.kind in ("api", "local") and e.endpoint_id in usable:
            group = "API models" if e.kind == "api" else "Local models"
            options.append(_option(f"api::{e.endpoint_id}::{e.model}", group,
                                   f"{e.model} · {e.endpoint_name}", e.model, e))
    for opt in options:
        if opt["cost_label"] is None:
            opt["cost_label"] = sub_note
    return options


def crew_preview(user: str | None) -> dict:
    """The roster the crew will get with the current settings (no files written)."""
    from src import model_roster

    settings = load_crew_settings()
    orch = parse_orchestrator_id(settings.get("orchestrator"))
    entries = [e for e in model_roster.roster(user) if e.kind in ("api", "local")]
    usable = {ep.id for ep, _kind, _provider in _crew_endpoints(user)}
    workers = [
        {"model": w["label"], "endpoint": "native CLI", "kind": "subscription", "recommended": False,
         "tier": "flagship", "cost_label": "flat-rate (plan limits)", "native": True}
        for w in _CLI_WORKERS
    ]
    seen = set()
    for e in entries:
        if e.endpoint_id not in usable or e.model in seen:
            continue
        if orch["kind"] == "api" and e.model == orch.get("model"):
            continue
        seen.add(e.model)
        workers.append({"model": e.model, "endpoint": e.endpoint_name, "kind": e.kind,
                        "recommended": e.recommended, "tier": e.tier, "traits": e.traits,
                        "cost_label": e.cost_label(), "cost_band": e.cost_band(), "native": False})
    native = workers[:len(_CLI_WORKERS)]
    rest = sorted(workers[len(_CLI_WORKERS):], key=lambda w: (not w["recommended"], w["model"].lower()))
    limit = int(settings.get("max_workers") or _MAX_WORKERS)
    return {"workers": native + rest[:limit], "omitted": max(0, len(rest) - limit)}


def setup_omnigent_routes(
    manager: OmnigentManager | None = None,
    native_manager: NativeOmnigentManager | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/omnigent", tags=["omnigent"])
    manager = manager or OmnigentManager()
    native_manager = native_manager or NativeOmnigentManager()

    @router.get("/status")
    def status(request: Request):
        data = manager.status()
        data["install"] = INSTALL_GUIDANCE
        data["ui_port"] = int(os.environ.get("OMNIGENT_UI_PORT") or os.environ.get("OMNIGENT_BRIDGE_PORT") or 6868)
        data["crew"] = {"name": _CREW_NAME, **load_crew_settings()}
        # Saved crew settings the running server has not picked up yet (e.g.
        # applied while it was still starting): the panel offers a restart.
        data["crew_pending_restart"] = crew_pending_restart(bool(data.get("running")))
        data["native"] = native_manager.status(model_ready=_has_visible_model_endpoint(request))
        return data

    @router.post("/server/start")
    def start_server(request: Request):
        require_admin(request)
        user = get_current_user(request)
        try:
            api = _install_api_models(user)
        except Exception as exc:
            logger.warning("omnigent start: install API models failed error_type=%s", type(exc).__name__)
            api = {"endpoints": 0, "models": 0, "error": "Could not install API model endpoints"}
        env_extra = dict(_builtin_agent_env() or {})
        try:
            creds = _gateway_credentials_env(user)
            if creds:
                env_extra.update(creds)
        except Exception:
            pass
        try:
            data = manager.start(env_extra=env_extra or None)
            try:
                data["refreshed_generated_agent_rows"] = _refresh_generated_session_agent_rows()
            except Exception:
                data["refreshed_generated_agent_rows"] = 0
        except Exception as exc:
            logger.warning("omnigent start: server start failed error_type=%s", type(exc).__name__)
            raise HTTPException(500, "Could not start the Omnigent server")
        data["install"] = INSTALL_GUIDANCE
        data["api_models"] = api
        return data

    @router.post("/server/stop")
    def stop_server(request: Request):
        require_admin(request)
        try:
            data = manager.stop()
        except Exception as exc:
            logger.warning("omnigent stop: server stop failed error_type=%s", type(exc).__name__)
            raise HTTPException(500, "Could not stop the Omnigent server")
        data["install"] = INSTALL_GUIDANCE
        return data

    @router.get("/sessions")
    def sessions(request: Request):
        # Conversational sessions live in the external Omnigent server.
        return manager.sessions()

    @router.get("/workers")
    def workers():
        data = manager.workers()
        data["native_workers"] = native_manager.worker_roster()
        data["presets"] = list(native_manager.presets().values())
        return data

    @router.get("/providers")
    def providers():
        return {"providers": _providers()}

    @router.get("/capabilities")
    def capabilities(request: Request):
        token_scopes = sorted(set(getattr(request.state, "api_token_scopes", []) or []))
        return {
            "integration": "omnigent",
            "native": native_manager.status(model_ready=_has_visible_model_endpoint(request)),
            "token_scopes": token_scopes,
            "providers": _providers(),
            "bridge": {
                "bundle": "/api/omnigent/bundle.tar.gz",
                "uses": "/api/codex/*",
                "required_env": ["ODYSSEUS_URL", "ODYSSEUS_API_TOKEN"],
            },
            "tools": [
                "odysseus_capabilities",
                "odysseus_todos",
                "odysseus_email_search",
                "odysseus_memory",
                "odysseus_calendar_events",
                "odysseus_documents",
                "odysseus_cookbook_tasks",
            ],
        }

    @router.get("/bundle.tar.gz")
    def bundle(request: Request):
        require_authenticated_request(request)
        root = Path(__file__).resolve().parent.parent / "integrations" / "omnigent"
        headers = {"Content-Disposition": 'attachment; filename="odysseus-omnigent-bundle.tar.gz"'}
        return Response(content=_bundle_bytes(root), media_type="application/gzip", headers=headers)

    @router.post("/launch")
    def launch(request: Request):
        # Install the owner's enabled API model endpoints into the bundled
        # Omnigent (as OpenAI-compatible gateway providers, marked API), then
        # boot its server (which serves Omnigent's own chat web UI). Degrades
        # gracefully when the Omnigent CLI isn't installed where Odysseus runs.
        # Admin only: Omnigent's agents run unsandboxed with a shell on the
        # Odysseus host, so launching it is the same trust level as the
        # server start route below.
        require_admin(request)
        user = get_current_user(request)
        if not _LAUNCH_LOCK.acquire(blocking=False):
            raise HTTPException(409, "Omnigent is already being launched. Try again in a moment.")
        try:
            return _launch_locked(user)
        finally:
            _LAUNCH_LOCK.release()

    def _launch_locked(user):
        launching_with = load_crew_settings()
        try:
            api = _install_api_models(user)
        except Exception as exc:
            logger.warning("omnigent launch: install API models failed error_type=%s", type(exc).__name__)
            api = {"endpoints": 0, "models": 0, "error": "Could not install API model endpoints"}
        # Restart so the freshly-generated crew + workers seed as built-in
        # agents (the picker only shows built-ins; they seed at startup), and
        # thread the imported gateway key into the server env so delegated/
        # spawned workers can authenticate (config.yaml providers aren't read on
        # the spawn path). The key only ever enters the server process env.
        env_extra = dict(_builtin_agent_env() or {})
        try:
            creds = _gateway_credentials_env(user)
            if creds:
                env_extra.update(creds)
        except Exception:
            pass
        try:
            data = manager.restart(env_extra=env_extra or None)
            try:
                mark_crew_launched(launching_with)
            except OSError:
                pass
            try:
                data["refreshed_generated_agent_rows"] = _refresh_generated_session_agent_rows()
            except Exception:
                data["refreshed_generated_agent_rows"] = 0
        except Exception as exc:
            logger.warning("omnigent launch: server restart failed error_type=%s", type(exc).__name__)
            data = manager.status()
            data["error"] = "Could not restart the Omnigent server"
        data["install"] = INSTALL_GUIDANCE
        data["api_models"] = api
        return data

    @router.get("/orchestrator")
    def get_orchestrator(request: Request):
        require_admin(request)
        user = get_current_user(request)
        return {
            "settings": load_crew_settings(),
            "options": orchestrator_options(user),
            "reasoning_efforts": list(_REASONING_EFFORTS),
            "max_workers_limit": _MAX_WORKERS,
            **crew_preview(user),
        }

    @router.post("/orchestrator")
    async def set_orchestrator(request: Request):
        """Choose who leads the crew; rewrites the crew and restarts a running server."""
        require_admin(request)
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        settings = load_crew_settings()
        if "orchestrator" in body:
            choice = str(body.get("orchestrator") or "")
            valid = {o["id"] for o in await asyncio.to_thread(orchestrator_options, user)}
            if choice not in valid:
                raise HTTPException(400, "That model is not available as an orchestrator.")
            settings["orchestrator"] = choice
        if "reasoning_effort" in body:
            if body["reasoning_effort"] not in _REASONING_EFFORTS:
                raise HTTPException(400, "Unknown reasoning effort")
            settings["reasoning_effort"] = body["reasoning_effort"]
        if "max_workers" in body:
            try:
                settings["max_workers"] = max(1, min(_MAX_WORKERS, int(body["max_workers"])))
            except (TypeError, ValueError):
                raise HTTPException(400, "max_workers must be a number")
        restart = bool(body.get("apply", True))
        if not restart:
            await asyncio.to_thread(save_crew_settings, settings)
            return {"settings": settings, "restarted": False}
        # Take the launch lock BEFORE saving: a choice saved while a launch is
        # in flight would be reported as applied while the server keeps the
        # crew it was started with.
        if not _LAUNCH_LOCK.acquire(blocking=False):
            raise HTTPException(409, "Omnigent is still starting. Apply again once it is up.")
        try:
            await asyncio.to_thread(save_crew_settings, settings)
            running = await asyncio.to_thread(lambda: bool(manager.status().get("running")))
            if running:
                data = await asyncio.to_thread(_launch_locked, user)
            else:
                # Not running: just rewrite the crew so the next launch uses it.
                api = await asyncio.to_thread(_install_api_models, user)
                data = {"running": False, "api_models": api}
        finally:
            _LAUNCH_LOCK.release()
        data["settings"] = settings
        data["restarted"] = running
        return data

    @router.post("/server/restart")
    def restart_server(request: Request):
        """Re-sync models and restart (picks up new endpoints, keys and settings)."""
        require_admin(request)
        user = get_current_user(request)
        if not _LAUNCH_LOCK.acquire(blocking=False):
            raise HTTPException(409, "Omnigent is already being launched. Try again in a moment.")
        try:
            return _launch_locked(user)
        finally:
            _LAUNCH_LOCK.release()

    return router
