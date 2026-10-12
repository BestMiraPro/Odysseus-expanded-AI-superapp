"""Conversation archive: your claude.ai, ChatGPT, Claude Code and Codex history.

Import sources (all text only; images, files and tool payloads are dropped):

- ``claude_ai``   - claude.ai data export (Settings -> Privacy -> Export data):
                    a zip holding ``conversations.json``.
- ``chatgpt``     - ChatGPT data export (Settings -> Data controls -> Export):
                    a zip holding ``conversations.json``.
- ``claude_code`` - Claude Code CLI transcripts, ``~/.claude/projects/*/*.jsonl``.
- ``codex``       - Codex CLI transcripts, ``~/.codex/sessions/**/rollout-*.jsonl``.

``import_path`` takes a zip, a single .json/.jsonl file or a folder and sniffs
what each file is, so one upload can mix sources. Importing the same
conversation again replaces it (keyed by its id in the source).

Search uses an SQLite FTS5 index when available and falls back to LIKE.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from sqlalchemy import text

logger = logging.getLogger(__name__)

SOURCES = ("claude_ai", "chatgpt", "claude_code", "codex")
SOURCE_LABELS = {
    "claude_ai": "claude.ai",
    "chatgpt": "ChatGPT",
    "claude_code": "Claude Code",
    "codex": "Codex",
}

# Guards for uploads: a zip may not inflate past this, and no single member
# past the per-file cap (a ChatGPT export's conversations.json can be large).
MAX_TOTAL_UNCOMPRESSED = int(os.getenv("ODYSSEUS_ARCHIVE_MAX_UNCOMPRESSED_BYTES", str(8 * 1024**3)))
MAX_MEMBER_BYTES = int(os.getenv("ODYSSEUS_ARCHIVE_MAX_FILE_BYTES", str(2 * 1024**3)))
MAX_MESSAGE_CHARS = 100_000


@dataclass
class ParsedConversation:
    source: str
    external_id: str
    title: str = ""
    project: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    messages: List[Tuple[str, str, Optional[datetime]]] = field(default_factory=list)  # (role, text, at)


# ── Time helpers ──

def _dt(value) -> Optional[datetime]:
    """Naive UTC datetime from an ISO string or epoch seconds (None if unparseable)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _clean(textual: str) -> str:
    textual = (textual or "").replace("\x00", "").strip()
    if len(textual) > MAX_MESSAGE_CHARS:
        textual = textual[:MAX_MESSAGE_CHARS] + "\n[... truncated ...]"
    return textual


def _title_from(messages, fallback: str) -> str:
    for role, body, _ in messages:
        if role == "user" and body:
            line = body.strip().splitlines()[0]
            return (line[:80] + "…") if len(line) > 80 else line
    return fallback


# ── claude.ai export ──

def parse_claude_ai(data: Any) -> Iterator[ParsedConversation]:
    for conv in data if isinstance(data, list) else []:
        if not isinstance(conv, dict):
            continue
        messages = []
        for msg in conv.get("chat_messages") or []:
            if not isinstance(msg, dict):
                continue
            role = {"human": "user", "assistant": "assistant"}.get(msg.get("sender"))
            if not role:
                continue
            parts = [b.get("text", "") for b in msg.get("content") or []
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            body = "\n\n".join(parts) if parts else (msg.get("text") or "")
            for att in msg.get("attachments") or []:
                if isinstance(att, dict) and att.get("extracted_content"):
                    body += f"\n\n[Attachment {att.get('file_name') or ''}]\n{att['extracted_content']}"
            body = _clean(body)
            if body:
                messages.append((role, body, _dt(msg.get("created_at"))))
        if not messages:
            continue
        yield ParsedConversation(
            source="claude_ai",
            external_id=str(conv.get("uuid") or uuid.uuid4()),
            title=(conv.get("name") or "").strip() or _title_from(messages, "claude.ai conversation"),
            started_at=_dt(conv.get("created_at")),
            updated_at=_dt(conv.get("updated_at")),
            messages=messages,
        )


# ── ChatGPT export ──

def _chatgpt_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    ctype = content.get("content_type")
    if ctype in ("text", "multimodal_text"):
        return "\n".join(p for p in content.get("parts") or [] if isinstance(p, str))
    if ctype == "code":
        return content.get("text") or ""
    return ""


def parse_chatgpt(data: Any) -> Iterator[ParsedConversation]:
    for conv in data if isinstance(data, list) else []:
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            continue
        mapping = conv["mapping"]
        # Walk the branch the user last saw: current_node up to the root.
        chain = []
        node_id = conv.get("current_node")
        seen = set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            chain.append(mapping[node_id])
            node_id = mapping[node_id].get("parent")
        messages = []
        for node in reversed(chain):
            msg = node.get("message") if isinstance(node, dict) else None
            if not isinstance(msg, dict):
                continue
            role = ((msg.get("author") or {}).get("role"))
            if role not in ("user", "assistant"):
                continue
            meta = msg.get("metadata") or {}
            if meta.get("is_visually_hidden_from_conversation"):
                continue
            body = _clean(_chatgpt_text(msg.get("content")))
            if body:
                messages.append((role, body, _dt(msg.get("create_time"))))
        if not messages:
            continue
        yield ParsedConversation(
            source="chatgpt",
            external_id=str(conv.get("conversation_id") or conv.get("id") or uuid.uuid4()),
            title=(conv.get("title") or "").strip() or _title_from(messages, "ChatGPT conversation"),
            started_at=_dt(conv.get("create_time")),
            updated_at=_dt(conv.get("update_time")),
            messages=messages,
        )


# ── Claude Code transcripts ──

def _block_text(content: Any, *, kinds=("text",)) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") in kinds and b.get("text"))
    return ""


_CLI_NOISE_RE = re.compile(r"^\s*<(command-|local-command-|system-reminder|environment_context|user_instructions|"
                           r"user-prompt-submit-hook|bash-)", re.IGNORECASE)


def parse_claude_code(lines: Iterable[str], name: str = "") -> Iterator[ParsedConversation]:
    """One Claude Code session file (``<session-id>.jsonl``)."""
    session_id = Path(name).stem if name else ""
    project = None
    summary = ""
    messages = []
    started = updated = None
    for raw in lines:
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "summary" and row.get("summary"):
            summary = str(row["summary"])
            continue
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain") or row.get("isMeta"):
            continue
        session_id = session_id or str(row.get("sessionId") or "")
        project = project or row.get("cwd")
        msg = row.get("message") or {}
        role = msg.get("role") or row.get("type")
        body = _clean(_block_text(msg.get("content")))
        if not body or _CLI_NOISE_RE.match(body):
            continue
        at = _dt(row.get("timestamp"))
        started = started or at
        updated = at or updated
        messages.append(("user" if role == "user" else "assistant", body, at))
    if messages:
        yield ParsedConversation(
            source="claude_code",
            external_id=session_id or str(uuid.uuid4()),
            title=summary or _title_from(messages, "Claude Code session"),
            project=project,
            started_at=started,
            updated_at=updated,
            messages=messages,
        )


# ── Codex transcripts ──

def parse_codex(lines: Iterable[str], name: str = "") -> Iterator[ParsedConversation]:
    """One Codex rollout file. Handles the wrapped ({type, payload}) and the
    older bare-item layouts."""
    session_id = ""
    project = None
    messages = []
    started = updated = None
    for raw in lines:
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        at = _dt(row.get("timestamp"))
        kind = row.get("type")
        item = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if kind == "session_meta":
            session_id = str(item.get("id") or session_id)
            project = item.get("cwd") or project
            started = started or _dt(item.get("timestamp")) or at
            continue
        if not kind and row.get("id") and "instructions" in row:   # old header line
            session_id = str(row.get("id"))
            started = started or at
            continue
        if item.get("type") != "message" or item.get("role") not in ("user", "assistant"):
            continue
        body = _clean(_block_text(item.get("content"), kinds=("input_text", "output_text", "text")))
        if not body or _CLI_NOISE_RE.match(body):
            continue
        started = started or at
        updated = at or updated
        messages.append((item["role"], body, at))
    if not session_id:
        match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", name or "")
        session_id = match.group(1) if match else (Path(name).stem if name else str(uuid.uuid4()))
    if messages:
        yield ParsedConversation(
            source="codex",
            external_id=session_id,
            title=_title_from(messages, "Codex session"),
            project=project,
            started_at=started,
            updated_at=updated,
            messages=messages,
        )


# ── Sniffing ──

def _sniff_jsonl(head: List[str]) -> Optional[str]:
    for raw in head:
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        if "sessionId" in row or (row.get("type") == "summary" and "leafUuid" in row):
            return "claude_code"
        if row.get("type") in ("session_meta", "response_item", "turn_context", "event_msg") and "payload" in row:
            return "codex"
        if row.get("type") == "message" and isinstance(row.get("content"), list):
            return "codex"
        if "instructions" in row and "id" in row:
            return "codex"
    return None


def parse_conversations_json(data: Any) -> Iterator[ParsedConversation]:
    if not isinstance(data, list):
        return
    first = next((c for c in data if isinstance(c, dict)), None)
    if first is None:
        return
    if "chat_messages" in first:
        yield from parse_claude_ai(data)
    elif "mapping" in first:
        yield from parse_chatgpt(data)


def _parse_jsonl_text(textual: str, name: str) -> Iterator[ParsedConversation]:
    lines = textual.splitlines()
    kind = _sniff_jsonl(lines[:20])
    if kind == "claude_code":
        yield from parse_claude_code(lines, name)
    elif kind == "codex":
        yield from parse_codex(lines, name)


def parse_file_bytes(name: str, payload: bytes) -> Iterator[ParsedConversation]:
    lower = name.lower()
    if lower.endswith(".jsonl"):
        yield from _parse_jsonl_text(payload.decode("utf-8", errors="replace"), name)
    elif lower.endswith(".json"):
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            return
        yield from parse_conversations_json(data)


_CONVERSATIONS_JSON_RE = re.compile(r"^conversations(?:[-_]\d+)?\.json$", re.IGNORECASE)


def _wanted(name: str) -> bool:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_CONVERSATIONS_JSON_RE.match(base)) or base.lower().endswith(".jsonl")


def _zip_members(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    total = 0
    picked = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        lower = info.filename.lower()
        base = lower.rsplit("/", 1)[-1]
        if not _wanted(info.filename):
            continue
        if base.startswith("._") or "/__macosx/" in f"/{lower}":
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"{info.filename} is larger than the import limit.")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("The archive expands past the import size limit.")
        picked.append(info)
    return picked


def parse_fileobj(fileobj, filename: str) -> Iterator[ParsedConversation]:
    """Conversations in an uploaded zip or single .json/.jsonl file."""
    if zipfile.is_zipfile(fileobj):
        fileobj.seek(0)
        with zipfile.ZipFile(fileobj) as zf:
            for info in _zip_members(zf):
                with zf.open(info) as fh:
                    yield from parse_file_bytes(info.filename, fh.read())
        return
    fileobj.seek(0)
    payload = fileobj.read(MAX_MEMBER_BYTES + 1)
    if len(payload) > MAX_MEMBER_BYTES:
        raise ValueError("The file is larger than the import limit.")
    yield from parse_file_bytes(filename or "upload.json", payload)


def parse_path(path: str) -> Iterator[ParsedConversation]:
    """Every conversation found in a zip, a .json/.jsonl file or a folder."""
    p = Path(path)
    if p.is_dir():
        for child in sorted(p.rglob("*")):
            if child.is_file() and _wanted(child.name):
                if child.stat().st_size > MAX_MEMBER_BYTES:
                    logger.warning("archive: skipped oversized %s", child.name)
                    continue
                yield from parse_file_bytes(child.name, child.read_bytes())
        return
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            for info in _zip_members(zf):
                with zf.open(info) as fh:
                    yield from parse_file_bytes(info.filename, fh.read())
        return
    yield from parse_file_bytes(p.name, p.read_bytes())


# ── Storage ──

def _fts_available(db) -> bool:
    try:
        if db.bind.dialect.name != "sqlite":
            return False
        db.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS archive_messages_fts USING fts5("
            "content, message_id UNINDEXED, conversation_id UNINDEXED, owner UNINDEXED)"
        ))
        return True
    except Exception as exc:
        logger.debug("archive FTS unavailable: %s", exc)
        return False


def _delete_conversations(db, conv_ids: List[str]) -> None:
    from core.database import ArchiveConversation, ArchiveMessage

    if not conv_ids:
        return
    for i in range(0, len(conv_ids), 500):
        chunk = conv_ids[i:i + 500]
        if _fts_available(db):
            db.execute(text("DELETE FROM archive_messages_fts WHERE conversation_id IN ("
                            + ",".join(f":c{j}" for j in range(len(chunk))) + ")"),
                       {f"c{j}": cid for j, cid in enumerate(chunk)})
        db.query(ArchiveMessage).filter(ArchiveMessage.conversation_id.in_(chunk)).delete(synchronize_session=False)
        db.query(ArchiveConversation).filter(ArchiveConversation.id.in_(chunk)).delete(synchronize_session=False)


def store(owner: Optional[str], conversations: Iterable[ParsedConversation]) -> Dict[str, int]:
    """Save parsed conversations for ``owner``; returns counts per source."""
    from core.database import ArchiveConversation, ArchiveMessage, SessionLocal

    counts: Dict[str, int] = {}
    db = SessionLocal()
    try:
        fts = _fts_available(db)
        batch = 0
        for conv in conversations:
            existing = (db.query(ArchiveConversation.id)
                        .filter(ArchiveConversation.owner.is_(None) if owner is None
                                else ArchiveConversation.owner == owner)
                        .filter(ArchiveConversation.source == conv.source,
                                ArchiveConversation.external_id == conv.external_id)
                        .all())
            _delete_conversations(db, [row.id for row in existing])
            cid = str(uuid.uuid4())
            db.add(ArchiveConversation(
                id=cid, owner=owner, source=conv.source, external_id=conv.external_id,
                title=(conv.title or "")[:300], project=(conv.project or None),
                started_at=conv.started_at, updated_at=conv.updated_at or conv.started_at,
                message_count=len(conv.messages),
            ))
            rows = [ArchiveMessage(conversation_id=cid, owner=owner, position=i, role=role,
                                   content=body, created_at=at)
                    for i, (role, body, at) in enumerate(conv.messages)]
            db.add_all(rows)
            db.flush()
            if fts:
                db.execute(text("INSERT INTO archive_messages_fts(content, message_id, conversation_id, owner) "
                                "VALUES (:content, :mid, :cid, :owner)"),
                           [{"content": r.content, "mid": r.id, "cid": cid, "owner": owner or ""} for r in rows])
            counts[conv.source] = counts.get(conv.source, 0) + 1
            batch += 1
            if batch % 200 == 0:
                db.commit()
        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def import_path(owner: Optional[str], path: str) -> Dict[str, int]:
    return store(owner, parse_path(path))


def import_fileobj(owner: Optional[str], fileobj, filename: str) -> Dict[str, int]:
    return store(owner, parse_fileobj(fileobj, filename))


def default_scan_dirs() -> List[str]:
    """Where this machine's CLI transcripts live (plus ODYSSEUS_TRANSCRIPT_DIRS)."""
    home = Path.home()
    dirs = [str(home / ".claude" / "projects"), str(home / ".codex" / "sessions")]
    extra = os.getenv("ODYSSEUS_TRANSCRIPT_DIRS", "")
    dirs += [d.strip() for d in extra.split(os.pathsep) if d.strip()]
    return dirs


def scan_local(owner: Optional[str], dirs: Optional[List[str]] = None) -> Dict[str, Any]:
    totals: Dict[str, int] = {}
    scanned = []
    for d in dirs or default_scan_dirs():
        if not Path(d).is_dir():
            continue
        scanned.append(d)
        for source, n in import_path(owner, d).items():
            totals[source] = totals.get(source, 0) + n
    return {"imported": totals, "scanned": scanned}


def _owner_clause(column, owner):
    return column.is_(None) if owner is None else column == owner


def stats(owner: Optional[str]) -> Dict[str, Any]:
    from sqlalchemy import func

    from core.database import ArchiveConversation, SessionLocal

    db = SessionLocal()
    try:
        rows = (db.query(ArchiveConversation.source, func.count(ArchiveConversation.id),
                         func.sum(ArchiveConversation.message_count), func.max(ArchiveConversation.imported_at))
                .filter(_owner_clause(ArchiveConversation.owner, owner))
                .group_by(ArchiveConversation.source).all())
        by_source = {
            source: {"label": SOURCE_LABELS.get(source, source), "conversations": int(n or 0),
                     "messages": int(m or 0), "last_import": last.isoformat() if last else None}
            for source, n, m, last in rows
        }
        return {"sources": by_source, "total": sum(v["conversations"] for v in by_source.values())}
    finally:
        db.close()


def delete_source(owner: Optional[str], source: str) -> int:
    from core.database import ArchiveConversation, SessionLocal

    db = SessionLocal()
    try:
        ids = [r.id for r in db.query(ArchiveConversation.id)
               .filter(_owner_clause(ArchiveConversation.owner, owner), ArchiveConversation.source == source).all()]
        _delete_conversations(db, ids)
        db.commit()
        return len(ids)
    finally:
        db.close()


# ── Search & read ──

def _fts_query(query: str) -> str:
    """User words as an FTS5 AND query of quoted terms (no operator injection)."""
    words = re.findall(r"[\w'’-]+", query or "", flags=re.UNICODE)
    return " ".join('"' + w.replace('"', '') + '"' for w in words[:12])


def search(owner: Optional[str], query: str, *, sources: Optional[List[str]] = None,
           limit: int = 10) -> List[Dict[str, Any]]:
    """Best-matching messages, newest conversations first among equal scores."""
    from core.database import ArchiveConversation, ArchiveMessage, SessionLocal

    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 10), 50))
    sources = [s for s in (sources or []) if s in SOURCES]
    db = SessionLocal()
    try:
        hits: List[Tuple[int, str]] = []   # (message id, snippet)
        fts_q = _fts_query(query)
        if fts_q and _fts_available(db):
            params: Dict[str, Any] = {"q": fts_q, "owner": owner or "", "lim": limit * 4}
            source_sql = ""
            if sources:
                source_sql = (" AND f.conversation_id IN (SELECT id FROM archive_conversations WHERE source IN ("
                              + ",".join(f":s{i}" for i in range(len(sources))) + "))")
                params.update({f"s{i}": s for i, s in enumerate(sources)})
            rows = db.execute(text(
                "SELECT f.message_id, snippet(archive_messages_fts, 0, '«', '»', ' … ', 28) "
                "FROM archive_messages_fts f WHERE archive_messages_fts MATCH :q AND f.owner = :owner"
                + source_sql + " ORDER BY bm25(archive_messages_fts) LIMIT :lim"
            ), params).fetchall()
            hits = [(int(r[0]), r[1]) for r in rows]
        else:
            q = (db.query(ArchiveMessage.id, ArchiveMessage.content)
                 .filter(_owner_clause(ArchiveMessage.owner, owner),
                         ArchiveMessage.content.ilike(f"%{query}%")))
            if sources:
                q = q.filter(ArchiveMessage.conversation_id.in_(
                    db.query(ArchiveConversation.id).filter(ArchiveConversation.source.in_(sources))))
            for mid, content in q.limit(limit * 4).all():
                i = content.lower().find(query.lower())
                start = max(0, i - 120)
                hits.append((mid, ("… " if start else "") + content[start:i + len(query) + 120] + " …"))
        if not hits:
            return []
        msgs = {m.id: m for m in db.query(ArchiveMessage).filter(ArchiveMessage.id.in_([h[0] for h in hits])).all()}
        conv_ids = {m.conversation_id for m in msgs.values()}
        convs = {c.id: c for c in db.query(ArchiveConversation).filter(ArchiveConversation.id.in_(conv_ids)).all()}
        out = []
        per_conv: Dict[str, int] = {}
        for mid, snippet in hits:
            m = msgs.get(mid)
            c = convs.get(m.conversation_id) if m else None
            if not m or not c or per_conv.get(c.id, 0) >= 2:
                continue
            per_conv[c.id] = per_conv.get(c.id, 0) + 1
            out.append({
                "conversation_id": c.id,
                "source": c.source,
                "source_label": SOURCE_LABELS.get(c.source, c.source),
                "title": c.title,
                "project": c.project,
                "date": (m.created_at or c.started_at).isoformat() if (m.created_at or c.started_at) else None,
                "role": m.role,
                "position": m.position,
                "snippet": (snippet or "").strip(),
            })
            if len(out) >= limit:
                break
        return out
    finally:
        db.close()


def read(owner: Optional[str], conversation_id: str, *, start: int = 0, limit: int = 20) -> Optional[Dict[str, Any]]:
    from core.database import ArchiveConversation, ArchiveMessage, SessionLocal

    db = SessionLocal()
    try:
        conv = (db.query(ArchiveConversation)
                .filter(ArchiveConversation.id == conversation_id,
                        _owner_clause(ArchiveConversation.owner, owner)).first())
        if conv is None:
            return None
        start = max(0, int(start or 0))
        limit = max(1, min(int(limit or 20), 100))
        rows = (db.query(ArchiveMessage).filter(ArchiveMessage.conversation_id == conv.id)
                .order_by(ArchiveMessage.position).offset(start).limit(limit).all())
        return {
            "conversation_id": conv.id,
            "source": conv.source,
            "source_label": SOURCE_LABELS.get(conv.source, conv.source),
            "title": conv.title,
            "project": conv.project,
            "started_at": conv.started_at.isoformat() if conv.started_at else None,
            "message_count": conv.message_count,
            "start": start,
            "messages": [{"position": r.position, "role": r.role, "content": r.content,
                          "at": r.created_at.isoformat() if r.created_at else None} for r in rows],
        }
    finally:
        db.close()


def format_search_results(results: List[Dict[str, Any]]) -> str:
    """Plain-text rendering for tools (MCP and agent)."""
    if not results:
        return "No matching conversations in the archive."
    lines = []
    for r in results:
        date = (r.get("date") or "")[:10]
        head = " · ".join(p for p in (r["source_label"], date, f"\"{r['title']}\"") if p)
        where = f" [{r['project']}]" if r.get("project") else ""
        lines.append(f"- {head}{where}\n"
                     f"  id={r['conversation_id']} message #{r['position']} ({r['role']}): {r['snippet']}")
    return "\n".join(lines)


def format_conversation(conv: Optional[Dict[str, Any]]) -> str:
    if not conv:
        return "Conversation not found."
    head = " · ".join(p for p in (conv["source_label"], f"\"{conv['title']}\"", (conv.get("started_at") or "")[:10],
                                  f"{conv['message_count']} messages") if p)
    if conv.get("project"):
        head += f" · {conv['project']}"
    body = [head, ""]
    for m in conv["messages"]:
        body.append(f"#{m['position']} {m['role'].upper()}:\n{m['content']}\n")
    end = conv["start"] + len(conv["messages"])
    if end < conv["message_count"]:
        body.append(f"[{conv['message_count'] - end} more messages; read again with start={end}]")
    return "\n".join(body)
