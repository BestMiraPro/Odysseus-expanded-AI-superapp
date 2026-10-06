"""AI Council routes: roster, threads, and the streaming ask endpoint.

A council run is an asyncio task owned by this process, not by the HTTP
request: closing the page does not stop it, the turn is saved when it ends,
and reopening the thread shows it (still running, or finished). Stop is an
explicit call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.database import CouncilSession, CouncilTurn, ModelEndpoint, SessionLocal, utcnow_naive
from src import council
from src.auth_helpers import get_current_user, owner_filter, require_user

logger = logging.getLogger(__name__)

SUBSCRIPTION_PROVIDERS = {"claude-subscription", "chatgpt-subscription", "copilot"}

# turn_id -> running task (this process only)
_RUNNING: Dict[str, asyncio.Task] = {}


class Seat(BaseModel):
    endpoint_id: str = Field(..., max_length=64)
    model: str = Field(..., max_length=300)


class AskRequest(BaseModel):
    question: str = Field(..., max_length=council.MAX_QUESTION_CHARS)
    members: List[Seat] = Field(..., max_length=council.MAX_MEMBERS)
    chairman: Seat
    mode: str = council.MODE_FULL


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


def _is_local_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in {"localhost", "host.docker.internal"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def classify_endpoint(ep) -> tuple[str, str]:
    """(kind, provider) where kind is subscription | api | local."""
    from src.llm_core import _detect_provider

    base = getattr(ep, "base_url", "") or ""
    try:
        provider = _detect_provider(base)
    except Exception:
        provider = "openai"
    if provider in SUBSCRIPTION_PROVIDERS:
        return "subscription", provider
    try:
        host = urlparse(base).hostname or ""
    except Exception:
        host = ""
    if _is_local_host(host):
        return "local", provider
    return "api", provider


def _chat_models(ep) -> List[str]:
    from src.endpoint_resolver import _NON_CHAT_MODEL, _endpoint_enabled_models

    return [m for m in _endpoint_enabled_models(ep)
            if not any(p in m.lower() for p in _NON_CHAT_MODEL)]


def _visible_endpoints(db, owner: Optional[str]):
    q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
    q = owner_filter(q, ModelEndpoint, owner)
    return [ep for ep in q.all() if (ep.model_type or "llm") == "llm"]


def roster_payload(owner: Optional[str], is_admin: bool) -> Dict[str, Any]:
    from src import claude_subscription

    db = SessionLocal()
    try:
        endpoints = []
        chatgpt = None
        for ep in _visible_endpoints(db, owner):
            kind, provider = classify_endpoint(ep)
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
    claude = claude_subscription.connection(owner)
    return {
        "endpoints": endpoints,
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


def resolve_seat(db, owner: Optional[str], seat: Seat) -> council.Member:
    from src.endpoint_resolver import build_chat_url, build_headers, resolve_endpoint_runtime

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
    try:
        base, api_key = resolve_endpoint_runtime(ep, owner=owner)
    except Exception as exc:
        logger.warning("council: credential resolution failed endpoint=%s error_type=%s",
                       ep.id, type(exc).__name__)
        raise council.CouncilError(f"{ep.name} needs reconnecting before it can sit on the council.", 401)
    kind, provider = classify_endpoint(ep)
    return council.Member(
        endpoint_id=ep.id,
        model=model,
        endpoint_name=ep.name or "",
        kind=kind,
        provider=provider,
        url=build_chat_url(base),
        headers=build_headers(api_key, base) or None,
    )


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
            if key in ("opinions", "reviews", "ranking", "labels", "config") and not isinstance(value, str):
                value = json.dumps(value)
            setattr(turn, key, value)
        db.commit()
    finally:
        db.close()


def _result_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "opinions": result.get("opinions") or [],
        "reviews": result.get("reviews") or [],
        "ranking": result.get("ranking") or [],
        "labels": result.get("labels") or {},
        "final": result.get("final") or None,
    }


def _history(db, owner: Optional[str], session_id: str) -> List[Dict[str, str]]:
    rows = (db.query(CouncilTurn)
            .filter(CouncilTurn.session_id == session_id, CouncilTurn.owner == owner,
                    CouncilTurn.status == "done")
            .order_by(CouncilTurn.created_at.asc()).all())
    return [{"question": r.question, "final": r.final or ""} for r in rows]


def _prepare_turn(owner: Optional[str], session_id: str, question: str, mode: str, body: AskRequest):
    """Validate the seats, record a running turn, and return what the run needs.

    Runs in a worker thread: resolving a ChatGPT seat may refresh its OAuth
    token over blocking HTTP.
    """
    db = SessionLocal()
    try:
        row = _get_session(db, owner, session_id)
        busy = [tid for tid, task in _RUNNING.items() if not task.done()]
        if busy:
            running_here = db.query(CouncilTurn.id).filter(CouncilTurn.id.in_(busy),
                                                           CouncilTurn.session_id == row.id).first()
            if running_here is not None:
                raise HTTPException(409, "This council is still deliberating. Stop it or wait.")
        try:
            members = [resolve_seat(db, owner, seat) for seat in body.members]
            chairman = resolve_seat(db, owner, body.chairman)
        except council.CouncilError as exc:
            raise HTTPException(exc.status, str(exc))
        history = _history(db, owner, row.id)
        config = {
            "members": [{"endpoint_id": s.endpoint_id, "model": s.model} for s in body.members],
            "chairman": {"endpoint_id": body.chairman.endpoint_id, "model": body.chairman.model},
            "mode": mode,
        }
        seats = {
            "members": [m.public(i) for i, m in enumerate(members)],
            "chairman": chairman.public("chair"),
            "mode": mode,
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
        return turn.id, members, chairman, history, seats
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

def setup_council_routes() -> APIRouter:
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

    @router.delete("/sessions/{session_id}")
    def delete_session(session_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            row = _get_session(db, owner, session_id)
            turns = db.query(CouncilTurn).filter(CouncilTurn.session_id == row.id,
                                                 CouncilTurn.owner == owner).all()
            for turn in turns:
                task = _RUNNING.get(turn.id)
                if task is not None:
                    task.cancel()
                db.delete(turn)
            db.delete(row)
            db.commit()
            return {"deleted": True}
        finally:
            db.close()

    @router.delete("/sessions/{session_id}/turns/{turn_id}")
    def delete_turn(session_id: str, turn_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            _get_session(db, owner, session_id)
            turn = db.query(CouncilTurn).filter(CouncilTurn.id == turn_id, CouncilTurn.session_id == session_id,
                                                CouncilTurn.owner == owner).first()
            if turn is None:
                raise HTTPException(404, "Turn not found")
            task = _RUNNING.get(turn.id)
            if task is not None:
                task.cancel()
            db.delete(turn)
            db.commit()
            return {"deleted": True}
        finally:
            db.close()

    @router.post("/sessions/{session_id}/turns/{turn_id}/stop")
    def stop_turn(session_id: str, turn_id: str, request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            _get_session(db, owner, session_id)
            turn = db.query(CouncilTurn).filter(CouncilTurn.id == turn_id, CouncilTurn.session_id == session_id,
                                                CouncilTurn.owner == owner).first()
            if turn is None:
                raise HTTPException(404, "Turn not found")
        finally:
            db.close()
        task = _RUNNING.get(turn_id)
        if task is None:
            return {"stopped": False}
        task.cancel()
        return {"stopped": True}

    @router.post("/sessions/{session_id}/ask")
    async def ask(session_id: str, request: Request, body: AskRequest):
        owner = _owner(request)
        question = (body.question or "").strip()
        if not question:
            raise HTTPException(400, "Ask the council a question.")
        mode = body.mode if body.mode in council.MODES else council.MODE_FULL
        if not body.members:
            raise HTTPException(400, "Seat at least one council member.")

        prepared = await asyncio.to_thread(_prepare_turn, owner, session_id, question, mode, body)
        turn_id, members, chairman, history, seats = prepared

        stream = _Broadcast()
        latest: Dict[str, Any] = {}

        async def checkpoint(result: Dict[str, Any]) -> None:
            latest.update(result)
            await asyncio.to_thread(_save_turn, turn_id, **_result_fields(result))

        async def runner() -> None:
            try:
                result = await council.run_council(
                    question=question, members=members, chairman=chairman, mode=mode,
                    history=history, emit=stream.emit, checkpoint=checkpoint,
                )
                status = "error" if result.get("error") and not result.get("final") else "done"
                fields = _result_fields(result)
                await asyncio.to_thread(_save_turn, turn_id, status=status,
                                        error=result.get("error"), **fields)
                await stream.emit({"type": "done", "status": status, "turn_id": turn_id,
                                   "final": fields["final"] or "", "error": result.get("error"),
                                   "final_fallback_member": result.get("final_fallback_member")})
            except asyncio.CancelledError:
                fields = _result_fields(latest) if latest else {}
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
