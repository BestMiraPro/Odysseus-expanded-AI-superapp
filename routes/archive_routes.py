"""Conversation archive API: import and search your claude.ai, ChatGPT,
Claude Code and Codex history (src/conversation_archive.py)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from core.middleware import require_admin
from src import conversation_archive as archive
from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)


def _owner(request: Request) -> Optional[str]:
    require_user(request)
    return effective_user(request) or None


def setup_archive_routes() -> APIRouter:
    router = APIRouter(prefix="/api/archive", tags=["archive"])

    @router.get("/stats")
    async def get_stats(request: Request):
        owner = _owner(request)
        out = await asyncio.to_thread(archive.stats, owner)
        out["scan_dirs"] = archive.default_scan_dirs()
        return out

    @router.post("/import")
    async def import_upload(request: Request, file: UploadFile = File(...)):
        """A claude.ai or ChatGPT export zip, or a zip/.jsonl of CLI transcripts."""
        owner = _owner(request)
        try:
            imported = await asyncio.to_thread(archive.import_fileobj, owner, file.file, file.filename or "")
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)[:300])
        if not imported:
            raise HTTPException(400, "No conversations found. Upload the export zip from claude.ai or "
                                     "ChatGPT, or a zip of ~/.claude/projects or ~/.codex/sessions.")
        return {"imported": imported, **(await asyncio.to_thread(archive.stats, owner))}

    @router.post("/scan")
    async def scan(request: Request):
        """Import the Claude Code / Codex transcripts on the Odysseus machine."""
        require_admin(request)
        owner = _owner(request)
        result = await asyncio.to_thread(archive.scan_local, owner)
        return {**result, **(await asyncio.to_thread(archive.stats, owner))}

    @router.get("/search")
    async def search(request: Request, q: str = Query(..., min_length=1, max_length=500),
                     source: Optional[str] = None, limit: int = 10):
        owner = _owner(request)
        sources = [s for s in (source or "").split(",") if s]
        return {"results": await asyncio.to_thread(archive.search, owner, q, sources=sources, limit=limit)}

    @router.get("/conversations/{conversation_id}")
    async def read(request: Request, conversation_id: str, start: int = 0, limit: int = 50):
        owner = _owner(request)
        conv = await asyncio.to_thread(archive.read, owner, conversation_id, start=start, limit=limit)
        if conv is None:
            raise HTTPException(404, "Conversation not found")
        return conv

    @router.delete("/sources/{source}")
    async def delete_source(request: Request, source: str):
        owner = _owner(request)
        if source not in archive.SOURCES:
            raise HTTPException(400, "Unknown source")
        removed = await asyncio.to_thread(archive.delete_source, owner, source)
        return {"removed": removed, **(await asyncio.to_thread(archive.stats, owner))}

    return router
