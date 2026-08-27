"""Omnigent integration routes for Odysseus."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
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


# Prefer a reasoning-capable general model as the default when installing API
# gateways into the bundled Omnigent.
_DEFAULT_MODEL_PREFS = ("glm-5", "glm", "qwen3", "deepseek", "llama")

# Upper bound on generated crew workers — large enough to install every model a
# typical gateway exposes (W&B lists ~29), bounded to guard against a runaway
# endpoint flooding the picker and the orchestrator's spawn roster.
_MAX_WORKERS = 40

# Curated "best option" gateway models that get their own crew entry. The
# broad `crew`/`crew-codex` can still delegate to every API model.
# Qwen 27B explicitly requested as a dedicated crew (cheap, good for 27B tier).
_BEST_API_MODEL_HINTS = (
    "glm-5", "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4",
    "kimi-k2", "qwen3-coder", "qwen3-235b", "qwen3-27b", "qwen2.5-27b",
    "qwen-27b", "27b", "minimax-m2", "nemotron-3-ultra",
)
_MAX_API_CREWS = 12
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


def _pick_best_api_models(ids: list[str], limit: int = _MAX_API_CREWS) -> list[str]:
    chosen: list[str] = []
    for hint in _BEST_API_MODEL_HINTS:
        for mid in ids:
            if hint in mid.lower() and mid not in chosen:
                chosen.append(mid)
    for mid in ids:
        if len(chosen) >= limit:
            break
        if mid not in chosen:
            chosen.append(mid)
    return chosen[:limit]


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


def _api_worker_spec(slug: str, mid: str, creds: tuple[str, str] | None) -> dict:
    return {
        "spec_version": 1,
        "name": slug,
        "description": f"{mid} worker (API model via the W&B gateway).",
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
) -> dict:
    worker_list = ", ".join(slugs) if slugs else "none"
    if lead_model:
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
    if primary_worker and not lead_model:
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


def _generate_crew(model_creds: dict[str, tuple[str, str]], default_model: str | None) -> int:
    """Write generated crew variants and their nested workers."""
    raw_ids = [mid for mid in model_creds if mid]
    # Rank rather than truncate: provider cache order is arbitrary and would
    # fill the roster with deprecated/unmeasured models while the best ones
    # went unused. Catalog ranking prefers measured benchmarks, then scale.
    try:
        declared = load_declared(path=str(Path(DATA_DIR) / "omnigent-model-costs.json"))
        ids = select_workers(raw_ids, declared, limit=_MAX_WORKERS)
    except Exception:
        ids = raw_ids[:_MAX_WORKERS]
    if not ids:
        return 0
    agents_root = Path(DATA_DIR) / "omnigent-home" / ".omnigent" / "agents"

    for pattern in ("crew", "crew-api-*", "crew-*"):
        for stale in agents_root.glob(pattern):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

    api_slugs: list[str] = []
    api_slug_by_model: dict[str, str] = {}
    used: set[str] = set()
    for mid in ids:
        slug = _model_slug(mid)
        while slug in used:
            slug += "-x"
        used.add(slug)
        api_slugs.append(slug)
        api_slug_by_model[mid] = slug
    api_specs = {slug: _api_worker_spec(slug, mid, model_creds.get(mid)) for slug, mid in zip(api_slugs, ids)}

    crew_slugs = [w["slug"] for w in _CLI_WORKERS] + api_slugs

    def write_full_roster_crew(name: str, description: str, executor: dict) -> None:
        crew_dir = agents_root / name
        for w in _CLI_WORKERS:
            _write_worker(crew_dir / "agents", w["slug"], _cli_worker_spec(w))
        for slug, spec in api_specs.items():
            _write_worker(crew_dir / "agents", slug, spec)
        _write_crew_file(crew_dir, _crew_config(
            name,
            description,
            executor,
            crew_slugs,
        ))

    claude_executor = {"type": "omnigent", "context_window": 1000000, "config": {"harness": "claude-sdk"}}
    codex_executor = {
        "type": "omnigent",
        "model": "gpt-5.6-sol",
        "context_window": 1000000,
        "config": {
            "harness": "codex-native",
            "yolo": True,
            "reasoning_effort": "xhigh",
        },
    }

    write_full_roster_crew(
        "crew",
        "Claude-brained orchestrator that delegates to Claude Code, Codex, and your W&B API-model workers.",
        claude_executor,
    )
    write_full_roster_crew(
        "crew-claude",
        "Claude-brained crew alias with Claude Code, Codex, and your W&B API-model workers.",
        claude_executor,
    )
    written = 2

    codex_dir = agents_root / "crew-codex"
    for slug, spec in api_specs.items():
        _write_worker(codex_dir / "agents", slug, spec)
    _write_crew_file(codex_dir, _crew_config(
        "crew-codex",
        "Codex-brained crew that can use its own tools and delegate to your W&B API-model workers.",
        codex_executor,
        api_slugs,
    ))
    written += 1

    # Per-model crews: API-model-brained, no Codex/Claude sub-agents to avoid
    # burning subscription usage. They run directly on their model and spawn
    # only other API workers if needed (currently lean: no sub-agents, broad
    # crews retain Codex/Claude for deep code work).
    for mid in _pick_best_api_models(ids):
        target_slug = api_slug_by_model[mid]
        crew_name = f"crew-{target_slug}"
        variant_dir = agents_root / crew_name
        model_crew_slugs: list[str] = []
        _write_crew_file(variant_dir, _crew_config(
            crew_name,
            f"{mid}-brained crew running directly on {mid} (no Codex/Claude fallback to save subscription usage).",
            _executor_block(mid, model_creds.get(mid)),
            model_crew_slugs,
            lead_model=mid,
        ))
        written += 1

    return written


def _generated_crew_dirs(agents_root: Path) -> list[Path]:
    crew_dirs: list[Path] = []
    broad = agents_root / "crew"
    if (broad / "config.yaml").exists():
        crew_dirs.append(broad)
    crew_dirs.extend(sorted(
        p for p in agents_root.glob("crew-*")
        if p.is_dir() and (p / "config.yaml").exists()
    ))
    return crew_dirs


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
            row = fresh.get(str(new_name))
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


def _install_api_models(user: str | None) -> dict:
    """Write every enabled Odysseus API endpoint into the bundled Omnigent as an
    OpenAI-compatible ``gateway`` provider (so all their models are available and
    marked API the moment the server boots).

    Keys are written inline into a 0600 config file: Omnigent's ``env:`` refs
    don't thread down to the harness that resolves the credential, so a file the
    harness reads directly is the reliable path. Lives in the persisted
    ``omnigent-home`` so it survives container recreates.
    """
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if user:
            q = owner_filter(q, ModelEndpoint, user)
        endpoints = [
            ep for ep in q.all()
            if (ep.model_type or "llm") == "llm" and ep.base_url and ep.api_key
        ]
        providers: dict[str, dict] = {}
        default_model: str | None = None
        model_count = 0
        # Ordered {model_id: (base_url, api_key)} so each generated worker can
        # bake its own endpoint's credentials inline (HOME-independent auth).
        model_creds: dict[str, tuple[str, str]] = {}
        used: set[str] = set()
        for idx, ep in enumerate(endpoints):
            slug = _provider_slug(ep.name or ep.base_url)
            while slug in used:
                slug += "-x"
            used.add(slug)
            ids = _endpoint_model_ids(ep)
            model_count += len(ids)
            ep_base = (ep.base_url or "").rstrip("/")
            for mid in ids:
                model_creds.setdefault(mid, (ep_base, ep.api_key))
            pick = _pick_default_model(ids)
            if default_model is None and pick:
                default_model = pick
            family = {"base_url": (ep.base_url or "").rstrip("/"), "api_key": ep.api_key, "wire_api": "chat"}
            if pick:
                family["models"] = {"default": pick}
            block: dict = {"kind": "gateway", "openai": family}
            if idx == 0:
                block["default"] = ["openai"]
            providers[slug] = block
    finally:
        db.close()
    if not providers:
        return {"endpoints": 0, "models": 0, "default_model": None}
    cfg_dir = Path(DATA_DIR) / "omnigent-home" / ".omnigent"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    cfg["providers"] = providers
    cfg["harness"] = "openai-agents"
    if default_model:
        cfg["model"] = default_model
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    try:
        os.chmod(cfg_path, 0o600)
    except Exception:
        pass
    workers = _generate_crew(model_creds, default_model)
    try:
        purged_builtin_rows = _purge_generated_builtin_agent_rows()
    except Exception:
        purged_builtin_rows = 0
    return {
        "endpoints": len(providers),
        "models": model_count,
        "default_model": default_model,
        "workers": workers,
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
        data["native"] = native_manager.status(model_ready=_has_visible_model_endpoint(request))
        return data

    @router.post("/server/start")
    def start_server(request: Request):
        require_admin(request)
        user = get_current_user(request)
        try:
            api = _install_api_models(user)
        except Exception as exc:
            api = {"endpoints": 0, "models": 0, "error": str(exc)}
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
            raise HTTPException(500, str(exc))
        data["install"] = INSTALL_GUIDANCE
        data["api_models"] = api
        return data

    @router.post("/server/stop")
    def stop_server(request: Request):
        require_admin(request)
        try:
            data = manager.stop()
        except Exception as exc:
            raise HTTPException(500, str(exc))
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
        require_authenticated_request(request)
        user = get_current_user(request)
        try:
            api = _install_api_models(user)
        except Exception as exc:
            api = {"endpoints": 0, "models": 0, "error": str(exc)}
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
                data["refreshed_generated_agent_rows"] = _refresh_generated_session_agent_rows()
            except Exception:
                data["refreshed_generated_agent_rows"] = 0
        except Exception as exc:
            data = manager.status()
            data["error"] = str(exc)
        data["install"] = INSTALL_GUIDANCE
        data["api_models"] = api
        return data

    return router
