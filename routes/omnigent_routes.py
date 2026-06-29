"""Omnigent integration routes for Odysseus."""

from __future__ import annotations

import json
import os
import tarfile
from io import BytesIO
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request, Response

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src.auth_helpers import get_current_user, owner_filter, require_authenticated_request
from src.constants import DATA_DIR
from src.omnigent_native import NativeOmnigentManager
from src.omnigent_manager import INSTALL_GUIDANCE, OmnigentManager


# Prefer a reasoning-capable general model as the default when installing API
# gateways into the bundled Omnigent.
_DEFAULT_MODEL_PREFS = ("glm-5", "glm", "qwen3", "deepseek", "llama")

# Upper bound on generated crew workers — large enough to install every model a
# typical gateway exposes (W&B lists ~29), bounded to guard against a runaway
# endpoint flooding the picker and the orchestrator's spawn roster.
_MAX_WORKERS = 40


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


def _generate_crew(model_creds: dict[str, tuple[str, str]], default_model: str | None) -> int:
    """Write a ``crew`` orchestrator + one worker per API model under the bundled
    Omnigent's ``~/.omnigent/agents/`` so the gateway models are delegatable
    sub-agents that can actually inspect and build on the user's repo.

    The orchestrator runs on ``claude-sdk`` with NO pinned model (so it works
    under the Claude brain and orchestrates reliably); each worker runs its own
    W&B model via openai-agents with inline auth (HOME-independent, see
    :func:`_executor_block`). Both carry an ``os_env`` block so they have real
    shell/file tools — without it they are bare chat LLMs that narrate and stop.
    Worker config files are chmod 0600 because they carry the key inline.
    """
    # Install every API model as a worker (the user asked for "all API models"),
    # bounded so a pathological endpoint can't flood the picker / spawn roster.
    ids = [mid for mid in model_creds if mid][:_MAX_WORKERS]
    if not ids:
        return 0
    crew = Path(DATA_DIR) / "omnigent-home" / ".omnigent" / "agents" / "crew"
    wroot = crew / "agents"
    slugs: list[str] = []
    used: set[str] = set()
    # Native coding CLIs first in the roster (the robust implementers), then the
    # API models. Claude Code works off the same login as the brain; Codex needs
    # a one-time `codex login` before it can run.
    for w in _CLI_WORKERS:
        wdir = wroot / w["slug"]
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "config.yaml").write_text(yaml.safe_dump(_cli_worker_spec(w), sort_keys=False))
        used.add(w["slug"])
        slugs.append(w["slug"])
    for mid in ids:
        slug = _model_slug(mid)
        while slug in used:
            slug += "-x"
        used.add(slug)
        wdir = wroot / slug
        wdir.mkdir(parents=True, exist_ok=True)
        cfg_file = wdir / "config.yaml"
        cfg_file.write_text(yaml.safe_dump({
            "spec_version": 1,
            "name": slug,
            "description": f"{mid} worker (API model via the W&B gateway).",
            "executor": _executor_block(mid, model_creds.get(mid)),
            "os_env": _os_env_block(),
            "prompt": _WORKER_PROMPT.format(slug=slug, mid=mid),
            "guardrails": {"policies": {"blast_radius": _blast_radius_guardrail()}},
        }, sort_keys=False))
        _chmod_600(cfg_file)
        slugs.append(slug)
    crew.mkdir(parents=True, exist_ok=True)
    crew_file = crew / "config.yaml"
    crew_file.write_text(yaml.safe_dump({
        "spec_version": 1,
        "name": "crew",
        "description": "Claude-brained orchestrator that delegates to Claude Code, Codex, and your W&B API-model workers.",
        # No model pinned: claude-sdk resolves the configured Claude default, so
        # the Claude brain doesn't choke on a W&B model id.
        "executor": {"type": "omnigent", "context_window": 1000000, "config": {"harness": "claude-sdk"}},
        "spawn": True,
        "async": True,
        "cancellable": True,
        "timers": True,
        "os_env": _os_env_block(),
        "terminals": {
            "shell": {"command": "bash", "allow_cwd_override": True, "os_env": _os_env_block()},
        },
        "prompt": _CREW_PROMPT.format(workers=", ".join(slugs)),
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
    }, sort_keys=False))
    return len(slugs)


def _builtin_agent_env() -> dict[str, str] | None:
    """Env that registers the generated crew + per-model workers as Omnigent
    built-in agents (the UI picker only lists built-ins, not ~/.omnigent/agents).
    `OMNIGENT_BUILTIN_AGENT_DIRS` is os.pathsep-separated and read at server
    startup."""
    crew = Path(DATA_DIR) / "omnigent-home" / ".omnigent" / "agents" / "crew"
    if not (crew / "config.yaml").exists():
        return None
    dirs = [str(crew)]
    for w in sorted((crew / "agents").glob("*")):
        if (w / "config.yaml").exists():
            dirs.append(str(w))
    return {"OMNIGENT_BUILTIN_AGENT_DIRS": os.pathsep.join(dirs)}


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
    return {
        "endpoints": len(providers),
        "models": model_count,
        "default_model": default_model,
        "workers": workers,
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
        try:
            data = manager.start()
        except Exception as exc:
            raise HTTPException(500, str(exc))
        data["install"] = INSTALL_GUIDANCE
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
        except Exception as exc:
            data = manager.status()
            data["error"] = str(exc)
        data["install"] = INSTALL_GUIDANCE
        data["api_models"] = api
        return data

    return router
