"""Memory transfer — copy memories (and skills) straight from another machine.

The file export/import in backup_routes moves memories between machines too,
but it needs a download, a file carried across, and an upload, and it drags
settings along with it. This is the direct path:

* ``GET  /api/memory-transfer/export`` — the SOURCE machine. Serves the
  caller's memories and skills. Reachable with an ``ody_`` API token that has
  ``memory:read`` (Settings → API Tokens), so the other machine needs no login.
* ``POST /api/memory-transfer/pull`` — the DESTINATION machine, admin only.
  Fetches the export from a source URL with that token and merges it in, with
  the same per-user dedup as the file import, so re-running a pull is safe.

Everything pulled is re-owned by the pulling user. The source's usernames
mean nothing on this machine; keeping them would store the rows under an
owner nobody here logs in as, and they would never show up.
"""

import asyncio
import copy
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from core.middleware import require_admin
from routes.backup_routes import (
    _validate_import_payload,
    merge_memories,
    merge_skills,
)
from routes.codex_routes import MEMORY_READ_SCOPES, _scope_owner
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

TRANSFER_KIND = "odysseus-memory-transfer"
TRANSFER_VERSION = 1
EXPORT_PATH = "/api/memory-transfer/export"
# A memory store is text; tens of MB is already an outlier. The cap keeps a
# wrong URL (a video, a disk image) from being buffered into RAM.
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
FETCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _export_owner(request: Request):
    """Whose data an export request may read.

    API tokens need ``memory:read`` and read their owner's rows. Browser
    sessions read the logged-in user's rows; with auth disabled that is None,
    which loads every row — the same as the file export.
    """
    if getattr(request.state, "api_token", False):
        return _scope_owner(request, MEMORY_READ_SCOPES)
    _scope_owner(request, MEMORY_READ_SCOPES)  # require_user for cookie callers
    return get_current_user(request)


# Any transfer endpoint the user might paste is trimmed back to the base
# address, so "http://h:7000/api/study-transfer/export" works as well as
# "http://h:7000".
_KNOWN_SUFFIX = re.compile(r"/api/[a-z-]+-transfer/[a-z-]+$")


def source_base_url(raw: Any) -> str:
    """Normalise what the user typed into the source's base address."""
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(400, "Source URL is required")
    url = raw.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Source URL must be an http(s) address, e.g. http://192.168.1.20:7000")
    if parsed.username or parsed.password:
        raise HTTPException(400, "Put the API token in the token field, not in the URL")
    if parsed.query or parsed.fragment:
        raise HTTPException(400, "Source URL must not carry a query string or fragment")
    path = _KNOWN_SUFFIX.sub("", parsed.path.rstrip("/"))
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _source_export_url(raw: Any) -> str:
    return source_base_url(raw) + EXPORT_PATH


def _reown(rows: list, user) -> list:
    """Copies of ``rows`` owned by ``user`` (untouched when auth is off)."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = copy.deepcopy(row)
        # The vector index is keyed by id; a row without one could never be
        # indexed or deleted from it.
        if not isinstance(row.get("id"), str) or not row["id"]:
            row["id"] = str(uuid.uuid4())
        if user:
            row["owner"] = user
        else:
            row.pop("owner", None)
        out.append(row)
    return out


async def fetch_from_source(url: str, token: str, *, params=None,
                            max_bytes: int = MAX_TRANSFER_BYTES,
                            scope: str = "memory:read") -> bytes:
    """GET ``url`` on the source machine with the transfer token.

    Redirects are not followed: the token is only meant for the host the user
    typed. The source's response body is never echoed back, so this cannot be
    used to read arbitrary internal pages; callers only accept a well-formed
    transfer payload or the file they asked for.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", url, headers=headers, params=params) as resp:
                if resp.status_code in (401, 403):
                    raise HTTPException(
                        502,
                        f"Source rejected the token (HTTP {resp.status_code}). "
                        f"It needs the {scope} scope.",
                    )
                if resp.status_code == 404:
                    raise HTTPException(
                        502,
                        "Source does not have this transfer endpoint (HTTP 404). "
                        "Update Odysseus on the source machine.",
                    )
                if 300 <= resp.status_code < 400:
                    raise HTTPException(
                        502,
                        f"Source redirected (HTTP {resp.status_code}); use its final address, "
                        "e.g. https:// instead of http://",
                    )
                if resp.status_code != 200:
                    raise HTTPException(502, f"Source returned HTTP {resp.status_code}")
                chunks = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            502, f"Source response is larger than the {max_bytes // (1024 * 1024)} MB limit")
                    chunks.append(chunk)
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "Timed out reaching the source machine")
    except httpx.HTTPError as e:
        logger.info("Transfer fetch failed: %s", type(e).__name__)
        raise HTTPException(
            502,
            "Could not reach the source machine. Check the address, that "
            "Odysseus is running there, and that its port is reachable from here.",
        )
    return b"".join(chunks)


def parse_transfer_payload(raw: bytes, kind: str) -> dict:
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(502, "Source did not return JSON — is that an Odysseus address?")
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise HTTPException(502, "Source did not return the expected transfer payload")
    return payload


async def _fetch_export(url: str, token: str, include_skills: bool) -> dict:
    """GET the source's memory export and return the parsed payload."""
    raw = await fetch_from_source(
        url, token, params={"include_skills": "1" if include_skills else "0"})
    return parse_transfer_payload(raw, TRANSFER_KIND)


def _index_memories(memory_vector, rows) -> None:
    """Embed newly pulled memories so retrieval can find them.

    The index is only rebuilt at startup when it is empty, so on a machine
    that already has memories, rows written straight to the store would
    otherwise never be searchable.
    """
    if memory_vector is None or not getattr(memory_vector, "healthy", False):
        return
    for row in rows:
        try:
            memory_vector.add(row["id"], row["text"])
        except Exception as e:
            logger.warning("Memory transfer: indexing %s failed: %s", row.get("id"), type(e).__name__)


def setup_memory_transfer_routes(memory_manager, skills_manager, memory_vector=None) -> APIRouter:
    router = APIRouter(prefix="/api/memory-transfer", tags=["memory-transfer"])

    @router.get("/export")
    def export_memories(request: Request, include_skills: bool = True):
        """Serve this user's memories (and skills) for another machine to pull."""
        owner = _export_owner(request)
        memories = memory_manager.load(owner=owner)
        payload = {
            "kind": TRANSFER_KIND,
            "version": TRANSFER_VERSION,
            "exported_at": datetime.now().isoformat(),
            "memories": [m for m in memories if isinstance(m, dict)],
        }
        if include_skills:
            payload["skills"] = skills_manager.load(owner=owner)
        return payload

    @router.post("/pull")
    async def pull_memories(request: Request, body: dict = Body(default_factory=dict)):
        """Fetch another machine's export and merge it into this one."""
        require_admin(request)
        user = get_current_user(request)

        url = _source_export_url(body.get("source_url"))
        token = body.get("token")
        if not isinstance(token, str) or not token.strip().startswith("ody_"):
            raise HTTPException(400, "An Odysseus API token (starts with ody_) is required")
        include_skills = body.get("include_skills", True) is not False
        dry_run = bool(body.get("dry_run", False))

        payload = await _fetch_export(url, token.strip(), include_skills)
        memories = payload.get("memories") or []
        skills = (payload.get("skills") or []) if include_skills else []
        incoming = {"memories": memories}
        if include_skills:
            incoming["skills"] = skills
        # Same validator as the file import: a malformed source writes nothing.
        _validate_import_payload(incoming)

        memories = _reown(memories, user)
        skills = _reown(skills, user)

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "source": url,
                "exported_at": payload.get("exported_at"),
                "memories": len(memories),
                "skills": len(skills),
            }

        landed: list = []
        added_memories = merge_memories(memory_manager, memories, user, landed) if memories else 0
        if landed:
            await asyncio.to_thread(_index_memories, memory_vector, landed)
        added_skills = merge_skills(skills_manager, skills, user) if skills else 0
        logger.info(
            "Memory transfer from %s: %d/%d memories, %d/%d skills added",
            urlparse(url).netloc, added_memories, len(memories), added_skills, len(skills),
        )
        return {
            "ok": True,
            "source": url,
            "exported_at": payload.get("exported_at"),
            "sections": [
                {"name": "memories", "added": added_memories,
                 "skipped": len(memories) - added_memories},
                {"name": "skills", "added": added_skills,
                 "skipped": len(skills) - added_skills},
            ],
        }

    return router
