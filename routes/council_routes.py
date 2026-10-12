"""AI Council routes: roster, threads, and the streaming ask endpoint.

A council run is an asyncio task owned by this process, not by the HTTP
request: closing the page does not stop it, the turn is saved when it ends,
and reopening the thread shows it (still running, or finished). Stop is an
explicit call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.database import CouncilSession, CouncilTurn, ModelEndpoint, SessionLocal, utcnow_naive
from src import budget, council, council_tools, model_roster, reasoning_effort
from src.auth_helpers import get_current_user, owner_filter, require_user

logger = logging.getLogger(__name__)

SUBSCRIPTION_PROVIDERS = model_roster.SUBSCRIPTION_PROVIDERS

# turn_id -> running task (this process only). Only touched on the event loop.
_RUNNING: Dict[str, asyncio.Task] = {}
# Sessions with an ask being prepared or running. Claimed on the event loop
# before any thread work, so two tabs cannot start two runs in one thread.
_ACTIVE_SESSIONS: set = set()


class Seat(BaseModel):
    endpoint_id: str = Field(..., max_length=64)
    model: str = Field(..., max_length=300)
    # Reasoning effort for this seat; None = the user's saved level for the
    # model (chat composer -> Effort), else the provider default.
    effort: Optional[str] = Field(None, max_length=16)


class AskRequest(BaseModel):
    question: str = Field(..., max_length=council.MAX_QUESTION_CHARS)
    members: List[Seat] = Field(..., max_length=council.MAX_MEMBERS)
    chairman: Seat
    mode: str = council.MODE_FULL
    # Set when the person has seen the cost estimate and agreed to a turn
    # above their single-action limit. It never overrides the monthly cap.
    budget_confirmed: bool = False
    # What members may do: off | research | full (src/council_tools.py), and
    # the folder their file tools work in. The Council page sends research by
    # default; an API caller that leaves it out keeps the text-only council.
    tools: str = council_tools.TOOLS_OFF
    workspace: Optional[str] = Field(None, max_length=1024)
    # Give members the person's shared memory repo and relevant saved
    # memories. The Council page sends it on by default; an API caller that
    # leaves it out shares nothing about the person.
    memory: bool = False


class EstimateRequest(BaseModel):
    question: str = Field("", max_length=council.MAX_QUESTION_CHARS)
    members: List[Seat] = Field(..., max_length=council.MAX_MEMBERS)
    chairman: Seat
    mode: str = council.MODE_FULL
    session_id: Optional[str] = Field(None, max_length=64)
    memory: bool = False


class SessionPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    config: Optional[Dict[str, Any]] = None


class SessionCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    config: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _owner(request: Request) -> Optional[str]:
    require_user(request)
    return get_current_user(request) or None


def _loads(raw, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except Exception:
        return default
    return value if isinstance(value, type(default)) else default


# Endpoint classification lives in src.model_roster, shared with the roster API,
# the Omnigent crew and the chat agent's ask_model tool.
classify_endpoint = model_roster.classify_endpoint
_chat_models = model_roster.chat_models
_visible_endpoints = model_roster.visible_endpoints


def roster_payload(owner: Optional[str], is_admin: bool) -> Dict[str, Any]:
    from src import claude_subscription

    db = SessionLocal()
    base_urls: Dict[str, str] = {}
    try:
        endpoints = []
        chatgpt = None
        for ep in _visible_endpoints(db, owner):
            kind, provider = classify_endpoint(ep)
            base_urls[ep.id] = ep.base_url or ""
            models = _chat_models(ep)
            if provider == "chatgpt-subscription" and chatgpt is None:
                chatgpt = {"endpoint_id": ep.id, "models": models}
            if not models:
                continue
            endpoints.append({
                "id": ep.id,
                "name": ep.name or ep.base_url,
                "kind": kind,
                "provider": provider,
                "models": models,
            })
    finally:
        db.close()
    order = {"subscription": 0, "api": 1, "local": 2}
    endpoints.sort(key=lambda e: (order.get(e["kind"], 3), e["name"].lower()))
    # Recommendation, tier and cost per seat option, for the picker badges.
    rows = [(e["id"], e["name"], m, e["kind"], e["provider"]) for e in endpoints for m in e["models"]]
    meta: Dict[str, Dict[str, Any]] = {}
    try:
        from src.reasoning_effort import levels_for
        for entry in model_roster.build_entries(rows):
            levels, default_effort = levels_for(entry.provider, base_urls.get(entry.endpoint_id, ""), entry.model)
            meta[entry.key] = {
                "recommended": entry.recommended,
                "tier": entry.tier,
                "traits": entry.traits,
                "cost_label": entry.cost_label(),
                "cost_band": entry.cost_band(),
                "billing": entry.billing,
                "effort_levels": list(levels),
                "default_effort": default_effort,
            }
    except Exception as exc:
        logger.warning("council roster metadata failed error_type=%s", type(exc).__name__)
    claude = claude_subscription.connection(owner)
    return {
        "endpoints": endpoints,
        "meta": meta,
        "can_connect": is_admin,
        "connections": {
            "claude": {
                "connected": bool(claude and claude.get("endpoint_id")),
                "endpoint_id": (claude or {}).get("endpoint_id"),
                "mode": (claude or {}).get("mode"),
                "cli_installed": bool(claude_subscription.find_cli()),
                "models": (claude or {}).get("models") or [],
            },
            "chatgpt": {
                "connected": chatgpt is not None,
                "endpoint_id": (chatgpt or {}).get("endpoint_id"),
                "models": (chatgpt or {}).get("models") or [],
            },
        },
        "modes": list(council.MODES),
        "max_members": council.MAX_MEMBERS,
    }


def resolve_seat(db, owner: Optional[str], seat: Seat, runtime: bool = True) -> council.Member:
    """The seat's endpoint, model, pricing and (with ``runtime``) credentials.

    ``runtime=False`` skips credential resolution, which can refresh an OAuth
    token over the network: cost estimates only need billing and prices.
    """
    from src.endpoint_resolver import build_chat_url, build_headers, resolve_endpoint_runtime
    from src.reasoning_effort import effective, user_levels, with_effort

    model = (seat.model or "").strip()
    if not model:
        raise council.CouncilError("Pick a model for every seat.")
    q = db.query(ModelEndpoint).filter(
        ModelEndpoint.id == seat.endpoint_id,
        ModelEndpoint.is_enabled == True,  # noqa: E712
    )
    ep = owner_filter(q, ModelEndpoint, owner).first()
    if ep is None:
        raise council.CouncilError("A selected model endpoint no longer exists.", 404)
    enabled = _chat_models(ep)
    if enabled and model not in enabled:
        raise council.CouncilError(f"{model} is not enabled on {ep.name}.")
    kind, provider = classify_endpoint(ep)
    # The seat's own level rides in its headers; without one the user's saved
    # level for the model applies. Either way the cards show the level used.
    seat_effort = effective(provider, ep.base_url or "", model, seat.effort)
    effort = seat_effort or effective(provider, ep.base_url or "", model, user_levels(owner).get(model))
    if not runtime:
        return council.Member(endpoint_id=ep.id, model=model, endpoint_name=ep.name or "", kind=kind,
                              provider=provider, context_tokens=_seat_context(model), effort=effort,
                              **_seat_pricing(ep, model, kind, provider))
    try:
        base, api_key = resolve_endpoint_runtime(ep, owner=owner)
    except Exception as exc:
        logger.warning("council: credential resolution failed endpoint=%s error_type=%s",
                       ep.id, type(exc).__name__)
        raise council.CouncilError(f"{ep.name} needs reconnecting before it can sit on the council.", 401)
    url = build_chat_url(base)
    return council.Member(
        endpoint_id=ep.id,
        model=model,
        endpoint_name=ep.name or "",
        kind=kind,
        provider=provider,
        url=url,
        headers=with_effort(build_headers(api_key, base), seat_effort) or None,
        context_tokens=_seat_context(model, url),
        effort=effort,
        **_seat_pricing(ep, model, kind, provider),
    )


def _seat_context(model: str, url: str = "") -> Optional[int]:
    """The seat's context window in tokens, or None when nothing reports it.

    With a chat URL the endpoint is asked (a local server reports the window it
    was started with); without one only the known-models table is read, so a
    cost estimate never waits on the network.
    """
    from src import model_context

    try:
        if not url:
            return model_context.known_context_length(model)
        ctx, known = model_context.get_context_length_known(url, model)
        return ctx if known else None
    except Exception as exc:
        logger.warning("council: context window lookup failed error_type=%s", type(exc).__name__)
        return None


def _seat_pricing(ep, model: str, kind: str, provider: str) -> Dict[str, Any]:
    """Billing and list price for one seat, from the shared model roster."""
    try:
        (entry,) = model_roster.build_entries([(ep.id, ep.name or "", model, kind, provider)])
    except Exception:
        return {"billing": "subscription" if kind == "subscription" else "local" if kind == "local" else "metered"}
    return {"billing": entry.billing, "input_per_mtok": entry.input_per_mtok,
            "output_per_mtok": entry.output_per_mtok}


def _session_query(db, owner: Optional[str]):
    return db.query(CouncilSession).filter(CouncilSession.owner == owner)


def _get_session(db, owner: Optional[str], session_id: str) -> CouncilSession:
    row = _session_query(db, owner).filter(CouncilSession.id == session_id).first()
    if row is None:
        raise HTTPException(404, "Council not found")
    return row


def _turn_json(turn: CouncilTurn) -> Dict[str, Any]:
    status = turn.status
    if status == "running" and turn.id not in _RUNNING:
        status = "interrupted"
    return {
        "id": turn.id,
        "question": turn.question,
        "config": _loads(turn.config, {}),
        "status": status,
        "opinions": _loads(turn.opinions, []),
        "reviews": _loads(turn.reviews, []),
        "ranking": _loads(turn.ranking, []),
        "labels": _loads(turn.labels, {}),
        "final": turn.final or "",
        "error": turn.error,
        "usage": _loads(turn.usage, {}),
        "chair": _loads(getattr(turn, "chair", None), None),
        "created_at": turn.created_at.isoformat() if turn.created_at else None,
    }


def _session_json(row: CouncilSession, turns: Optional[List[CouncilTurn]] = None, turn_count: int = 0) -> Dict[str, Any]:
    data = {
        "id": row.id,
        "title": row.title,
        "config": _loads(row.config, {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "turn_count": turn_count,
    }
    if turns is not None:
        data["turns"] = [_turn_json(t) for t in turns]
        data["turn_count"] = len(turns)
    return data


def _save_turn(turn_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        turn = db.query(CouncilTurn).filter(CouncilTurn.id == turn_id).first()
        if turn is None:
            return
        for key, value in fields.items():
            if key in ("opinions", "reviews", "ranking", "labels", "config", "usage") and not isinstance(value, str):
                value = json.dumps(value)
            elif key == "chair" and value is not None and not isinstance(value, str):
                value = json.dumps(value)
            setattr(turn, key, value)
        db.commit()
    finally:
        db.close()


def _settled(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of per-member results; anything still streaming is marked stopped."""
    out = []
    for call in calls or []:
        item = {k: v for k, v in call.items() if k != "pending"}
        if call.get("pending"):
            item["error"] = "Stopped before this member finished."
            item["stopped"] = True
        out.append(item)
    return out


def _result_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    final = result.get("final") or None
    chair = result.get("chair") if isinstance(result.get("chair"), dict) else None
    if not final and chair and chair.get("pending") and chair.get("text"):
        # Keep a synthesis that was cut off: the user watched it stream in.
        final = chair["text"].rstrip() + "\n\n*(stopped before the chairman finished)*"
    return {
        "opinions": _settled(result.get("opinions")),
        "reviews": _settled(result.get("reviews")),
        "ranking": result.get("ranking") or [],
        "labels": result.get("labels") or {},
        "final": final,
        "usage": council.usage_totals(result),
        "chair": {"tools": chair.get("tools") or []} if chair and chair.get("tools") else None,
    }


def _history(db, owner: Optional[str], session_id: str) -> List[Dict[str, str]]:
    rows = (db.query(CouncilTurn)
            .filter(CouncilTurn.session_id == session_id, CouncilTurn.owner == owner,
                    CouncilTurn.status == "done")
            .order_by(CouncilTurn.created_at.asc()).all())
    return [{"question": r.question, "final": r.final or ""} for r in rows]


def _expected_outputs(db, owner: Optional[str]) -> Dict[str, int]:
    """This user's typical reply lengths per stage, from their recent turns."""
    rows = (db.query(CouncilTurn)
            .filter(CouncilTurn.owner == owner, CouncilTurn.status == "done")
            .order_by(CouncilTurn.created_at.desc()).limit(20).all())
    turns = [{"opinions": _loads(r.opinions, []), "reviews": _loads(r.reviews, []),
              "usage": _loads(r.usage, {})} for r in rows]
    return council.observed_output_tokens(turns)


def _estimate(db, owner: Optional[str], members, chairman, mode: str, question: str,
              history: List[Dict[str, str]], context: str = "") -> Dict[str, Any]:
    return council.estimate_turn(members, chairman, mode, question, history,
                                 expected=_expected_outputs(db, owner), context=context)


# Set by setup_council_routes: the chat processor whose memory selection
# (core pinned facts, then the best matches) the council reuses.
_CHAT_PROCESSOR = None
RECALL_QUERY_CHARS = 4000


def _memory_context(owner: Optional[str], question: str, history: List[Dict[str, str]],
                    *, wait: bool = True) -> str:
    """What the council may know about the person: shared memory and relevant saved memories.

    Empty when the person turned memory off. ``wait`` reads the shared memory
    repo first when nothing is cached yet (the ask runs off the event loop);
    estimates use the cache only.
    """
    from routes.prefs_routes import _load_for_user
    from src import shared_memory
    from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message

    try:
        if not _load_for_user(owner).get("memory_enabled", True):
            return ""
    except Exception:
        pass
    parts = []
    try:
        shared = shared_memory.context_text(owner, wait=wait)
    except Exception as exc:
        logger.warning("council: shared memory unavailable error_type=%s", type(exc).__name__)
        shared = ""
    if shared:
        parts.append(shared)
    texts: List[str] = []
    if _CHAT_PROCESSOR is not None:
        # The first question is usually the brief; later ones can be as short
        # as "1 - all", which matches nothing on its own.
        first = history[0].get("question", "") if history else ""
        query = f"{first}\n{question}".strip()[-RECALL_QUERY_CHARS:]
        try:
            entries = _CHAT_PROCESSOR.memory_manager.load(owner=owner)
            pinned, recalled = _CHAT_PROCESSOR.select_memories(query, entries)
            texts = [m["text"] for m in [*pinned, *recalled] if m.get("text")]
        except Exception as exc:
            logger.warning("council: memory recall failed error_type=%s", type(exc).__name__)
    if texts:
        body = "Saved memories about the user. Use them as background where relevant:\n" + "\n".join(
            f"- {t}" for t in texts)
        parts += [UNTRUSTED_CONTEXT_POLICY, untrusted_context_message("saved memory", body)["content"]]
    return "\n\n".join(parts)


def _budget_gate(owner: Optional[str], estimate: Dict[str, Any], confirmed: bool) -> None:
    """Refuse a turn the monthly cap forbids; ask first above the action limit."""
    decision = budget.check(owner, estimate.get("total_usd"), metered=estimate.get("metered", False),
                            what="This council turn")
    if not decision.allowed:
        raise HTTPException(402, {"code": "budget_blocked", "message": decision.reason,
                                  "estimate": estimate, "budget": decision.to_dict()})
    if decision.confirm and not confirmed:
        raise HTTPException(402, {"code": "budget_confirm", "message": decision.reason,
                                  "estimate": estimate, "budget": decision.to_dict()})


def _record_turn_spend(owner: Optional[str], session_id: str, members, chairman,
                       result: Dict[str, Any]) -> None:
    for member, stage, usage, cost, estimated in council.billable_calls(result, members, chairman):
        if member.billing != "metered":
            continue
        budget.record(owner, source="council", model=member.model, usage=usage, price=member.price,
                      endpoint_id=member.endpoint_id, endpoint_name=member.endpoint_name,
                      session_id=session_id, cost_usd=cost, estimated=estimated)


def _tool_settings(request: Request, owner: Optional[str], body: AskRequest) -> council_tools.ToolSettings:
    """The members' tool level and workspace for this turn.

    Full tools (shell, edits) and a workspace (host files) need the same
    privilege as chat's file and shell tools: an admin or a single-user install.
    """
    from src.tool_security import owner_is_admin_or_single_user

    mode = council_tools.normalize_mode(body.tools)
    privileged = owner_is_admin_or_single_user(get_current_user(request))
    if mode == council_tools.TOOLS_FULL and not privileged:
        raise HTTPException(403, "Full tools (shell commands and file edits) need an admin account.")
    workspace = None
    requested = (body.workspace or "").strip()
    if requested and mode != council_tools.TOOLS_OFF:
        if not privileged:
            raise HTTPException(403, "A workspace folder needs an admin account.")
        from src.tool_execution import vet_workspace

        workspace = vet_workspace(requested) or None
        if workspace is None:
            raise HTTPException(400, "That workspace folder can't be used: it must be an existing, "
                                     "non-sensitive folder that is not a filesystem root.")
    return council_tools.ToolSettings(mode=mode, owner=owner, workspace=workspace)


def _prepare_turn(owner: Optional[str], session_id: str, question: str, mode: str, body: AskRequest,
                  tools: Optional[council_tools.ToolSettings] = None):
    """Validate the seats, record a running turn, and return what the run needs.

    Runs in a worker thread: resolving a ChatGPT seat may refresh its OAuth
    token over blocking HTTP.
    """
    db = SessionLocal()
    try:
        row = _get_session(db, owner, session_id)
        try:
            members = [resolve_seat(db, owner, seat) for seat in body.members]
            chairman = resolve_seat(db, owner, body.chairman)
        except council.CouncilError as exc:
            raise HTTPException(exc.status, str(exc))
        history = _history(db, owner, row.id)
        context = _memory_context(owner, question, history) if body.memory else ""
        try:
            estimate = _estimate(db, owner, members, chairman, mode, question, history, context)
        except Exception as exc:
            # No estimate: still enforce a reached cap, but never fail the ask.
            logger.warning("council: estimate failed error_type=%s", type(exc).__name__)
            estimate = {"total_usd": None, "metered": any(m.billing == "metered" for m in [*members, chairman])}
        _budget_gate(owner, estimate, body.budget_confirmed)
        tools_mode = tools.mode if tools else council_tools.TOOLS_OFF
        workspace = tools.workspace if tools else None

        def _seat_config(s: Seat) -> Dict[str, str]:
            seat = {"endpoint_id": s.endpoint_id, "model": s.model}
            level = reasoning_effort.normalize(s.effort)
            if level:
                seat["effort"] = level
            return seat

        config = {
            "members": [_seat_config(s) for s in body.members],
            "chairman": _seat_config(body.chairman),
            "mode": mode,
            "tools": tools_mode,
            "workspace": workspace or "",
            "memory": body.memory,
        }
        seats = {
            "members": [m.public(i) for i, m in enumerate(members)],
            "chairman": chairman.public("chair"),
            "mode": mode,
            "tools": tools_mode,
            "workspace": workspace or "",
            "memory": bool(context),
        }
        turn = CouncilTurn(
            id=uuid.uuid4().hex,
            session_id=row.id,
            owner=owner,
            question=question,
            config=json.dumps(seats),
            status="running",
        )
        db.add(turn)
        row.config = json.dumps(config)
        if row.title == "New council":
            row.title = question.splitlines()[0][:80]
        row.updated_at = utcnow_naive()
        db.commit()
        return turn.id, members, chairman, history, seats, context
    finally:
        db.close()


class _Broadcast:
    """Fan-out of one run's events to the request that started it (if still there)."""

    def __init__(self):
        self.queue: Optional[asyncio.Queue] = asyncio.Queue()

    async def emit(self, event: Dict[str, Any]) -> None:
        if self.queue is not None:
            self.queue.put_nowait(event)

    def detach(self) -> None:
        self.queue = None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def setup_council_routes(chat_processor=None) -> APIRouter:
    global _CHAT_PROCESSOR
    if chat_processor is not None:
        _CHAT_PROCESSOR = chat_processor
    router = APIRouter(prefix="/api/council", tags=["council"])

    def _is_admin(request: Request) -> bool:
        from core.middleware import require_admin

        try:
            require_admin(request)
            return True
        except HTTPException:
            return False

    @router.get("/roster")
    def roster(request: Request):
        owner = _owner(request)
        return roster_payload(owner, _is_admin(request))

    def _estimate_for(owner, body: EstimateRequest) -> Dict[str, Any]:
        mode = body.mode if body.mode in council.MODES else council.MODE_FULL
        db = SessionLocal()
        try:
            try:
                members = [resolve_seat(db, owner, seat, runtime=False) for seat in body.members]
                chairman = resolve_seat(db, owner, body.chairman, runtime=False)
            except council.CouncilError as exc:
                raise HTTPException(exc.status, str(exc))
            history: List[Dict[str, str]] = []
            if body.session_id:
                row = _session_query(db, owner).filter(CouncilSession.id == body.session_id).first()
                if row is not None:
                    history = _history(db, owner, row.id)
            question = (body.question or "").strip()
            context = _memory_context(owner, question, history, wait=False) if body.memory else ""
            estimate = _estimate(db, owner, members, chairman, mode, question, history, context)
        finally:
            db.close()
        decision = budget.check(owner, estimate.get("total_usd"), metered=estimate["metered"],
                                what="This council turn")
        return {"estimate": estimate, "budget": decision.to_dict()}

    @router.post("/estimate")
    async def estimate(request: Request, body: EstimateRequest):
        owner = _owner(request)
        if not body.members:
            return {"estimate": None, "budget": await asyncio.to_thread(budget.status, owner)}
        return await asyncio.to_thread(_estimate_for, owner, body)

    @router.get("/sessions")
    def list_sessions(request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            rows = _session_query(db, owner).order_by(CouncilSession.updated_at.desc()).limit(200).all()
            counts: Dict[str, int] = {}
            if rows:
                from sqlalchemy import func

                for sid, n in (db.query(CouncilTurn.session_id, func.count(CouncilTurn.id))
                               .filter(CouncilTurn.session_id.in_([r.id for r in rows]))
                               .group_by(CouncilTurn.session_id).all()):
                    counts[sid] = n
            return {"sessions": [_session_json(r, turn_count=counts.get(r.id, 0)) for r in rows]}
        finally:
            db.close()

    @router.post("/sessions")
    def create_session(request: Request, body: SessionCreate):
        owner = _owner(request)
        db = SessionLocal()
        try:
            config = body.config
            if config is None:
                # New councils start with the seats of the most recent one.
                last = _session_query(db, owner).order_by(CouncilSession.updated_at.desc()).first()
                config = _loads(last.config, {}) if last else {}
            row = CouncilSession(
                id=uuid.uuid4().hex,
                owner=owner,
                title=(body.title or "").strip() or "New council",
                config=json.dumps(config or {}),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _session_json(row, turns=[])
        finally:
            db.close()

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            row = _get_session(db, owner, session_id)
            turns = (db.query(CouncilTurn)
                     .filter(CouncilTurn.session_id == row.id, CouncilTurn.owner == owner)
                     .order_by(CouncilTurn.created_at.asc()).all())
            return _session_json(row, turns=turns)
        finally:
            db.close()

    @router.patch("/sessions/{session_id}")
    def patch_session(session_id: str, request: Request, body: SessionPatch):
        owner = _owner(request)
        db = SessionLocal()
        try:
            row = _get_session(db, owner, session_id)
            if body.title is not None and body.title.strip():
                row.title = body.title.strip()
            if body.config is not None:
                row.config = json.dumps(body.config)
            db.commit()
            db.refresh(row)
            return _session_json(row)
        finally:
            db.close()

    def _delete_session_rows(owner, session_id) -> List[str]:
        db = SessionLocal()
        try:
            row = _get_session(db, owner, session_id)
            turns = db.query(CouncilTurn).filter(CouncilTurn.session_id == row.id,
                                                 CouncilTurn.owner == owner).all()
            ids = [t.id for t in turns]
            for turn in turns:
                db.delete(turn)
            db.delete(row)
            db.commit()
            return ids
        finally:
            db.close()

    def _owned_turn_id(owner, session_id, turn_id, delete: bool = False) -> str:
        db = SessionLocal()
        try:
            _get_session(db, owner, session_id)
            turn = db.query(CouncilTurn).filter(CouncilTurn.id == turn_id, CouncilTurn.session_id == session_id,
                                                CouncilTurn.owner == owner).first()
            if turn is None:
                raise HTTPException(404, "Turn not found")
            if delete:
                db.delete(turn)
                db.commit()
            return turn_id
        finally:
            db.close()

    def _cancel(turn_id: str) -> bool:
        # Async routes run on the event loop, so cancelling here is safe.
        task = _RUNNING.get(turn_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        owner = _owner(request)
        for turn_id in await asyncio.to_thread(_delete_session_rows, owner, session_id):
            _cancel(turn_id)
        return {"deleted": True}

    @router.delete("/sessions/{session_id}/turns/{turn_id}")
    async def delete_turn(session_id: str, turn_id: str, request: Request):
        owner = _owner(request)
        await asyncio.to_thread(_owned_turn_id, owner, session_id, turn_id, True)
        _cancel(turn_id)
        return {"deleted": True}

    @router.post("/sessions/{session_id}/turns/{turn_id}/stop")
    async def stop_turn(session_id: str, turn_id: str, request: Request):
        owner = _owner(request)
        await asyncio.to_thread(_owned_turn_id, owner, session_id, turn_id)
        return {"stopped": _cancel(turn_id)}

    @router.post("/sessions/{session_id}/ask")
    async def ask(session_id: str, request: Request, body: AskRequest):
        owner = _owner(request)
        question = (body.question or "").strip()
        if not question:
            raise HTTPException(400, "Ask the council a question.")
        mode = body.mode if body.mode in council.MODES else council.MODE_FULL
        if not body.members:
            raise HTTPException(400, "Seat at least one council member.")

        tools = _tool_settings(request, owner, body)

        if session_id in _ACTIVE_SESSIONS:
            raise HTTPException(409, "This council is still deliberating. Stop it or wait.")
        _ACTIVE_SESSIONS.add(session_id)
        try:
            prepared = await asyncio.to_thread(_prepare_turn, owner, session_id, question, mode, body, tools)
        except BaseException:
            _ACTIVE_SESSIONS.discard(session_id)
            raise
        turn_id, members, chairman, history, seats, context = prepared

        stream = _Broadcast()
        state: Dict[str, Any] = {}
        finished = {"saved": False}

        async def checkpoint(result: Dict[str, Any]) -> None:
            await asyncio.to_thread(_save_turn, turn_id, **_result_fields(result))

        async def runner() -> None:
            try:
                with reasoning_effort.for_owner(owner):
                    result = await council.run_council(
                        question=question, members=members, chairman=chairman, mode=mode,
                        history=history, emit=stream.emit, checkpoint=checkpoint, state=state,
                        stream_fn=council_tools.stream_fn_for(tools), context=context,
                    )
                status = "error" if result.get("error") and not result.get("final") else "done"
                fields = _result_fields(result)
                # Set before the save: a Stop that lands while it is being
                # written must not overwrite the finished turn.
                finished["saved"] = True
                await asyncio.to_thread(_save_turn, turn_id, status=status,
                                        error=result.get("error"), **fields)
                await stream.emit({"type": "done", "status": status, "turn_id": turn_id,
                                   "final": fields["final"] or "", "error": result.get("error"),
                                   "usage": fields["usage"],
                                   "final_fallback_member": result.get("final_fallback_member")})
            except asyncio.CancelledError:
                if not finished["saved"]:
                    fields = _result_fields(state) if state else {}
                    await asyncio.to_thread(_save_turn, turn_id, status="cancelled",
                                            error="Stopped before the council finished.", **fields)
                await stream.emit({"type": "done", "status": "cancelled", "turn_id": turn_id})
            except Exception as exc:
                logger.exception("council run failed")
                await asyncio.to_thread(_save_turn, turn_id, status="error",
                                        error="The council run failed unexpectedly.")
                await stream.emit({"type": "done", "status": "error", "turn_id": turn_id,
                                   "error": "The council run failed unexpectedly."})
                del exc
            finally:
                _RUNNING.pop(turn_id, None)
                _ACTIVE_SESSIONS.discard(session_id)
                # Every call that ran is billed once, whether the turn
                # finished, was stopped or failed.
                try:
                    await asyncio.to_thread(_record_turn_spend, owner, session_id, members, chairman, state)
                except Exception as exc:
                    logger.warning("council: spend not recorded error_type=%s", type(exc).__name__)
                await stream.emit(None)

        task = asyncio.create_task(runner(), name=f"council-{turn_id}")
        _RUNNING[turn_id] = task

        async def events():
            yield f"data: {json.dumps({'type': 'turn', 'turn_id': turn_id, 'session_id': session_id, **seats})}\n\n"
            try:
                while True:
                    queue = stream.queue
                    if queue is None:
                        break
                    event = await queue.get()
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                # The run keeps going without a listener; it saves itself.
                stream.detach()

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
