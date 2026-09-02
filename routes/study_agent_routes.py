# routes/study_agent_routes.py
"""HTTP surface of the in-app Study agent (src/study_agent.py).

  GET    /api/study/agent/capabilities            model in use, code-tool eligibility, runtime
  GET    /api/study/agent/threads                 the caller's chat threads (newest first)
  POST   /api/study/agent/threads {deck_id?}      new thread
  GET    /api/study/agent/threads/{id}/messages   messages in UI shape
  DELETE /api/study/agent/threads/{id}
  POST   /api/study/agent/chat                    {thread_id?, message, deck_id?, allow_code?} -> SSE
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import study_agent
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)


class ThreadCreate(BaseModel):
    deck_id: Optional[str] = None
    title: Optional[str] = None


class ChatIn(BaseModel):
    message: str
    thread_id: Optional[str] = None
    deck_id: Optional[str] = None
    allow_code: bool = False


def setup_study_agent_routes() -> APIRouter:
    router = APIRouter(prefix="/api/study/agent", tags=["study-agent"])

    @router.get("/capabilities")
    def capabilities(request: Request):
        from src.tool_security import owner_is_admin_or_single_user
        user = get_current_user(request)
        model = ""
        try:
            from routes.study_routes import _resolve_study_model
            _url, model, _h = _resolve_study_model(user, prefer_text=True)
        except HTTPException as e:
            model = f"(none: {e.detail})"
        return {
            "model": model,
            "code_tools_allowed": owner_is_admin_or_single_user(user),
            "code_root": study_agent.code_root(),
            "runtime": "docker" if study_agent.running_in_docker() else "native",
            "tools": [t.name for t in study_agent.TOOLS.values()],
        }

    @router.get("/threads")
    def threads(request: Request):
        return {"threads": study_agent.list_threads(get_current_user(request))}

    @router.post("/threads")
    def create_thread(request: Request, body: ThreadCreate):
        return study_agent.create_thread(get_current_user(request), deck_id=body.deck_id, title=body.title)

    @router.get("/threads/{thread_id}/messages")
    def thread_messages(request: Request, thread_id: str):
        return {"messages": study_agent.thread_messages_for_ui(get_current_user(request), thread_id)}

    @router.delete("/threads/{thread_id}")
    def delete_thread(request: Request, thread_id: str):
        study_agent.delete_thread(get_current_user(request), thread_id)
        return {"ok": True}

    @router.post("/chat")
    async def chat(request: Request, body: ChatIn):
        user = get_current_user(request)
        text = (body.message or "").strip()
        if not text:
            raise HTTPException(400, "message is required")
        thread_id = body.thread_id
        if thread_id:
            study_agent.get_thread(user, thread_id)  # ownership check
        else:
            thread_id = study_agent.create_thread(user, deck_id=body.deck_id)["id"]

        async def _gen():
            yield study_agent._sse({"type": "thread", "thread_id": thread_id})
            try:
                async for chunk in study_agent.run_study_agent(
                        user, thread_id, text[:20000], deck_id=body.deck_id, allow_code=body.allow_code):
                    yield chunk
            except Exception as e:  # keep the stream well-formed
                logger.exception("study agent stream failed")
                yield study_agent._sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
                yield study_agent.DONE

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
