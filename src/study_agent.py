# src/study_agent.py
"""The Study agent — a focused tool-calling loop that lives inside the Study
pane and can act on the learner's behalf.

What it can do:
- manage subjects, materials, questions, cards and exams through dedicated,
  owner-scoped tools (the same service functions the HTTP routes use);
- run the AI pipelines (extract / author questions, transcribe scanned PDFs,
  study notes, subject overview, bank maintenance);
- tutor from the learner's own materials (`search_materials` + paged
  `get_material`, so answers cite material + page instead of guessing);
- for admins who opt in (``allow_code``): read and change the app's code in
  the code root through the existing confined file/shell tools.

Design (see docs/superpowers/specs/2026-09-02-study-agent-design.md):
- one thread = one conversation, persisted in study_agent_threads/messages;
- native OpenAI-style function calling, with a fenced ``tool_call`` fallback
  for models that cannot emit tool_calls;
- token discipline: a terse static app map, live state as counts, paged tool
  outputs, old tool results trimmed out of the history window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException

from core.database import (
    SessionLocal,
    StudyAgentMessage,
    StudyAgentThread,
    StudyAttempt,
    StudyCard,
    StudyDeck,
    StudyExam,
    StudyMaterial,
    StudyQuestion,
    StudyReview,
)
from src.constants import BASE_DIR

logger = logging.getLogger(__name__)

MAX_ROUNDS = 15                 # tool rounds per user turn
HISTORY_MESSAGES = 40           # persisted messages sent back to the model
TOOL_RESULT_MODEL_CHARS = 6000  # tool output the model sees (per call)
TOOL_RESULT_UI_CHARS = 20000    # tool output kept in the UI event
OLD_TOOL_RESULT_CHARS = 300     # tool results older than the last 2 rounds
MATERIAL_PAGE_CHARS = 6000      # get_material page size
REPLY_MAX_TOKENS = 6000         # leaves room for hidden reasoning tokens

CODE_TOOL_NAMES = ("read_file", "write_file", "edit_file", "grep", "glob", "ls", "bash")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def code_root() -> str:
    """Directory the code tools are confined to: ``ODYSSEUS_CODE_DIR`` when set
    (e.g. a bind-mounted checkout inside Docker), else the app directory."""
    raw = os.environ.get("ODYSSEUS_CODE_DIR") or BASE_DIR
    return os.path.realpath(raw.rstrip("/\\") or BASE_DIR)


def running_in_docker() -> bool:
    return os.path.exists("/.dockerenv") or bool(os.environ.get("ODYSSEUS_IN_DOCKER"))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

Handler = Callable[[Optional[str], Dict[str, Any]], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Handler
    properties: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)
    destructive: bool = False
    code: bool = False

    def schema(self) -> Dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.properties,
                               "required": list(self.required)},
            },
        }


TOOLS: Dict[str, ToolSpec] = {}


def tool(name: str, description: str, properties: Optional[Dict] = None,
         required: Optional[List[str]] = None, *, destructive: bool = False,
         code: bool = False):
    """Register an async handler ``(owner, args) -> result`` as an agent tool."""
    def deco(fn: Handler) -> Handler:
        TOOLS[name] = ToolSpec(name=name, description=description, handler=fn,
                               properties=properties or {}, required=required or [],
                               destructive=destructive, code=code)
        return fn
    return deco


def tool_schemas(include_code: bool = False) -> List[Dict]:
    return [t.schema() for t in TOOLS.values() if include_code or not t.code]


def _s(desc: str, **extra) -> Dict:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str) -> Dict:
    return {"type": "integer", "description": desc}


def _b(desc: str) -> Dict:
    return {"type": "boolean", "description": desc}


def _sr():
    """The study route module (lazy: keeps src -> routes a soft dependency)."""
    from routes import study_routes
    return study_routes


def _owned(q, model, owner):
    return q.filter(model.owner == owner) if owner is not None else q


def _require(args: Dict, *keys: str) -> None:
    missing = [k for k in keys if args.get(k) in (None, "", [])]
    if missing:
        raise HTTPException(400, f"missing required argument(s): {', '.join(missing)}")


def _confirmed(args: Dict) -> None:
    if not args.get("confirm"):
        raise HTTPException(400, "This action is destructive. Ask the user to confirm, "
                                 "then call again with confirm=true.")


# ------------------------------------------------------------------ subjects

@tool("list_subjects", "List the learner's subjects (decks) with material, question and card counts.")
async def _list_subjects(owner, args):
    sr = _sr()
    db = SessionLocal()
    try:
        decks = _owned(db.query(StudyDeck).filter(StudyDeck.archived == False), StudyDeck, owner).all()  # noqa: E712
        out = []
        for d in decks:
            c = sr._deck_counts(db, owner, d)
            n_mat = _owned(db.query(StudyMaterial).filter(StudyMaterial.deck_id == d.id),
                           StudyMaterial, owner).count()
            out.append({"id": d.id, "name": d.name, "description": d.description,
                        "materials": n_mat, "questions": c["q_total"], "questions_due": c["q_due"],
                        "questions_new": c["q_new"], "cards": c["total"], "cards_due": c["due_count"],
                        "new_per_day": d.new_per_day, "has_overview": bool(d.overview)})
        return {"subjects": out}
    finally:
        db.close()


@tool("create_subject", "Create a subject (deck).",
      {"name": _s("Subject name"), "description": _s("Optional description")}, ["name"])
async def _create_subject(owner, args):
    _require(args, "name")
    db = SessionLocal()
    try:
        deck = StudyDeck(id=str(uuid.uuid4()), owner=owner, name=str(args["name"]).strip()[:200],
                         description=(args.get("description") or None), new_per_day=15, retention="0.9")
        db.add(deck)
        db.commit()
        return {"id": deck.id, "name": deck.name}
    finally:
        db.close()


@tool("update_subject", "Rename a subject, change its description or its new-cards-per-day cap.",
      {"deck_id": _s("Subject id"), "name": _s("New name"), "description": _s("New description"),
       "new_per_day": _i("New flashcards introduced per day")}, ["deck_id"])
async def _update_subject(owner, args):
    _require(args, "deck_id")
    sr = _sr()
    db = SessionLocal()
    try:
        deck = sr._get_deck(db, args["deck_id"], owner)
        if args.get("name"):
            deck.name = str(args["name"]).strip()[:200]
        if "description" in args and args["description"] is not None:
            deck.description = str(args["description"])
        if args.get("new_per_day") is not None:
            deck.new_per_day = max(0, int(args["new_per_day"]))
        db.commit()
        return {"ok": True, "id": deck.id, "name": deck.name}
    finally:
        db.close()


@tool("delete_subject", "Delete a subject with ALL its materials, questions, cards and history. Destructive: needs confirm=true after the user agreed.",
      {"deck_id": _s("Subject id"), "confirm": _b("Must be true")}, ["deck_id", "confirm"], destructive=True)
async def _delete_subject(owner, args):
    _require(args, "deck_id")
    _confirmed(args)
    sr = _sr()
    db = SessionLocal()
    try:
        deck = sr._get_deck(db, args["deck_id"], owner)
        name = deck.name
        db.query(StudyReview).filter(StudyReview.deck_id == deck.id).delete()
        db.query(StudyAttempt).filter(StudyAttempt.deck_id == deck.id).delete()
        db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck.id).delete()
        db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck.id).delete()
        db.delete(deck)  # cards cascade
        db.commit()
        return {"ok": True, "deleted": name}
    finally:
        db.close()


# ------------------------------------------------------------------ materials

_PAGE_RE = re.compile(r"\[Page (\d+) (?:text|image \d+ text)\]:")


def _page_at(text: str, index: int) -> Optional[int]:
    """Document page of a character offset, from the [Page N text] markers."""
    page = None
    for m in _PAGE_RE.finditer(text):
        if m.start() > index:
            break
        page = int(m.group(1))
    return page


def _page_slice(text: str, page: int) -> Optional[str]:
    marks = [(int(m.group(1)), m.start()) for m in _PAGE_RE.finditer(text)]
    for i, (n, start) in enumerate(marks):
        if n == page:
            end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
            return text[start:end]
    return None


@tool("list_materials", "List a subject's materials (name, kind, category theory|exam, size, question count, notes).",
      {"deck_id": _s("Subject id")}, ["deck_id"])
async def _list_materials(owner, args):
    _require(args, "deck_id")
    sr = _sr()
    db = SessionLocal()
    try:
        deck = sr._get_deck(db, args["deck_id"], owner)
        rows = sr.material_rows_with_counts(db, deck.id, owner)
        keep = ("id", "name", "kind", "category", "char_count", "page_count", "thin_text",
                "question_count", "has_summary")
        return {"materials": [{k: r.get(k) for k in keep} for r in rows]}
    finally:
        db.close()


@tool("get_material", "Read a material's text, paged (6000 chars per call) or one PDF page via `page`. Use search_materials first to find where something is.",
      {"material_id": _s("Material id"), "offset": _i("Character offset to start from (default 0)"),
       "page": _i("Document page number (PDFs with [Page N] markers)")}, ["material_id"])
async def _get_material(owner, args):
    _require(args, "material_id")
    sr = _sr()
    db = SessionLocal()
    try:
        m = sr._get_material(db, args["material_id"], owner)
        text = m.content or ""
        name, cat = m.name, sr._material_category(m)
    finally:
        db.close()
    if not text.strip():
        return {"name": name, "total_chars": 0, "text": "",
                "note": "This material has no text layer. For a scanned/formula PDF call transcribe_material first."}
    if args.get("page"):
        chunk = _page_slice(text, int(args["page"]))
        if chunk is None:
            return {"name": name, "error": f"no [Page {args['page']}] marker in this material"}
        return {"name": name, "category": cat, "page": int(args["page"]),
                "text": _clip(chunk, MATERIAL_PAGE_CHARS)}
    off = max(0, int(args.get("offset") or 0))
    chunk = text[off:off + MATERIAL_PAGE_CHARS]
    nxt = off + MATERIAL_PAGE_CHARS
    return {"name": name, "category": cat, "total_chars": len(text), "offset": off,
            "page_at_offset": _page_at(text, off), "text": chunk,
            "next_offset": nxt if nxt < len(text) else None}


@tool("search_materials", "Find where a term/regex appears in a subject's materials: snippets with material name and page. Ground tutoring answers with this.",
      {"deck_id": _s("Subject id"), "query": _s("Case-insensitive substring or regex"),
       "material_id": _s("Limit to one material"), "max_hits": _i("Max hits (default 20)")},
      ["deck_id", "query"])
async def _search_materials(owner, args):
    _require(args, "deck_id", "query")
    sr = _sr()
    query = str(args["query"])
    try:
        rx = re.compile(query, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(query), re.IGNORECASE)
    limit = max(1, min(50, int(args.get("max_hits") or 20)))
    db = SessionLocal()
    try:
        sr._get_deck(db, args["deck_id"], owner)
        q = _owned(db.query(StudyMaterial).filter(StudyMaterial.deck_id == args["deck_id"]),
                   StudyMaterial, owner)
        if args.get("material_id"):
            q = q.filter(StudyMaterial.id == args["material_id"])
        mats = q.order_by(StudyMaterial.created_at.asc()).all()
        hits = []
        for m in mats:
            text = m.content or ""
            for hit in rx.finditer(text):
                a, b = max(0, hit.start() - 240), min(len(text), hit.end() + 240)
                hits.append({"material_id": m.id, "material": m.name,
                             "category": sr._material_category(m),
                             "page": _page_at(text, hit.start()),
                             "snippet": " ".join(text[a:b].split())})
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
        return {"hits": hits, "materials_searched": len(mats)}
    finally:
        db.close()


@tool("add_material", "Add pasted text as a material of a subject.",
      {"deck_id": _s("Subject id"), "name": _s("Material name"), "text": _s("The material text"),
       "category": _s("theory (notes/lectures) or exam (past paper / problem set)", enum=["theory", "exam"])},
      ["deck_id", "name", "text"])
async def _add_material(owner, args):
    _require(args, "deck_id", "text")
    row = _sr().create_material_record(owner, args["deck_id"], name=args.get("name"),
                                       text=args["text"], category=args.get("category"))
    return {k: row.get(k) for k in ("id", "name", "kind", "category", "char_count")}


@tool("set_material_category", "Mark a material as theory (only generates questions) or exam/practice (only extracts questions).",
      {"material_id": _s("Material id"), "category": _s("theory | exam", enum=["theory", "exam"])},
      ["material_id", "category"])
async def _set_material_category(owner, args):
    _require(args, "material_id", "category")
    sr = _sr()
    from src.study_ai import MATERIAL_CATEGORIES
    if args["category"] not in MATERIAL_CATEGORIES:
        raise HTTPException(400, f"category must be one of {MATERIAL_CATEGORIES}")
    db = SessionLocal()
    try:
        m = sr._get_material(db, args["material_id"], owner)
        m.category = args["category"]
        db.commit()
        return {"ok": True, "id": m.id, "category": m.category}
    finally:
        db.close()


@tool("remove_material", "Remove a material (optionally with the questions extracted from it). Destructive: needs confirm=true.",
      {"material_id": _s("Material id"), "with_questions": _b("Also delete its questions"),
       "confirm": _b("Must be true")}, ["material_id", "confirm"], destructive=True)
async def _remove_material(owner, args):
    _require(args, "material_id")
    _confirmed(args)
    sr = _sr()
    db = SessionLocal()
    try:
        m = sr._get_material(db, args["material_id"], owner)
        removed_q = 0
        if args.get("with_questions"):
            ids = [r.id for r in db.query(StudyQuestion.id).filter(StudyQuestion.material_id == m.id).all()]
            if ids:
                db.query(StudyAttempt).filter(StudyAttempt.question_id.in_(ids)).delete(synchronize_session=False)
                removed_q = db.query(StudyQuestion).filter(StudyQuestion.id.in_(ids)).delete(synchronize_session=False)
        name = m.name
        db.delete(m)
        db.commit()
        return {"ok": True, "removed": name, "questions_removed": removed_q}
    finally:
        db.close()


# ------------------------------------------------------------------ questions

def _q_row(q: StudyQuestion, full: bool = False) -> Dict:
    d = {"id": q.id, "number": q.number, "qtype": q.qtype, "topic": q.topic,
         "difficulty": q.difficulty, "state": q.state, "suspended": bool(q.suspended),
         "material_id": q.material_id, "reps": q.reps or 0, "lapses": q.lapses or 0}
    if full:
        d.update({"question": q.question, "context": q.context,
                  "options": json.loads(q.options) if q.options else None,
                  "correct_index": q.correct_index, "reference": q.reference,
                  "origin": q.origin, "due": q.due.isoformat() if q.due else None})
    else:
        d["question"] = (q.question or "")[:160]
    return d


@tool("list_questions", "List a subject's questions (compact rows: id, number, type, topic, state, first 160 chars). Filter by material, text or type; paged.",
      {"deck_id": _s("Subject id"), "material_id": _s("Only questions from this material"),
       "query": _s("Substring to search in the question text"), "qtype": _s("mcq | open", enum=["mcq", "open"]),
       "limit": _i("Rows per page (default 30, max 100)"), "offset": _i("Page offset")}, ["deck_id"])
async def _list_questions(owner, args):
    _require(args, "deck_id")
    sr = _sr()
    limit = max(1, min(100, int(args.get("limit") or 30)))
    offset = max(0, int(args.get("offset") or 0))
    db = SessionLocal()
    try:
        sr._get_deck(db, args["deck_id"], owner)
        q = _owned(db.query(StudyQuestion).filter(StudyQuestion.deck_id == args["deck_id"]),
                   StudyQuestion, owner)
        if args.get("material_id"):
            q = q.filter(StudyQuestion.material_id == args["material_id"])
        if args.get("qtype") in ("mcq", "open"):
            q = q.filter(StudyQuestion.qtype == args["qtype"])
        if args.get("query"):
            q = q.filter(StudyQuestion.question.ilike(f"%{args['query']}%"))
        total = q.count()
        rows = q.order_by(StudyQuestion.created_at.asc()).offset(offset).limit(limit).all()
        return {"total": total, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "questions": [_q_row(r) for r in rows]}
    finally:
        db.close()


@tool("get_question", "Full detail of one question (text, context, options, answer, reference).",
      {"question_id": _s("Question id")}, ["question_id"])
async def _get_question(owner, args):
    _require(args, "question_id")
    db = SessionLocal()
    try:
        return _q_row(_sr()._get_question(db, args["question_id"], owner), full=True)
    finally:
        db.close()


def save_questions(owner, deck_id: str, items: List[Dict], *, material_id: Optional[str] = None,
                   origin: str = "user") -> Dict:
    """Normalize + dedupe question dicts (extractor schema) and save them to a
    deck, skipping anything already in the bank. Returns {created, duplicates, ids}."""
    from src.study_ai import dedupe_questions, normalize_questions, question_key
    sr = _sr()
    questions = dedupe_questions(normalize_questions(items))
    db = SessionLocal()
    try:
        sr._get_deck(db, deck_id, owner)
        if material_id:
            sr._get_material(db, material_id, owner)
        existing = {question_key(t) for (t,) in
                    db.query(StudyQuestion.question).filter(StudyQuestion.deck_id == deck_id).all()}
        now, ids, dup = _now(), [], 0
        for q in questions:
            key = question_key(q["question"])
            if key in existing:
                dup += 1
                continue
            existing.add(key)
            row = StudyQuestion(
                id=str(uuid.uuid4()), owner=owner, deck_id=deck_id, material_id=material_id,
                qtype=q["qtype"], question=q["question"], context=q.get("context"),
                options=json.dumps(q["options"]) if q["options"] else None,
                correct_index=q["correct_index"], reference=q["reference"],
                topic=q["topic"], difficulty=q["difficulty"], number=q.get("number"),
                origin=origin, state="new", due=now)
            db.add(row)
            ids.append(row.id)
        db.commit()
        return {"created": len(ids), "duplicates": dup, "ids": ids,
                "rejected": max(0, len(items) - len(questions))}
    finally:
        db.close()


@tool("add_questions", "Write questions into a subject's bank. Each item: {type: 'mcq'|'open', question, options?, correct_index?, reference, topic?, difficulty?, number?, context?}. MCQs without a resolvable correct answer are rejected.",
      {"deck_id": _s("Subject id"), "material_id": _s("Material the questions belong to (optional)"),
       "questions": {"type": "array", "description": "Question objects", "items": {"type": "object"}}},
      ["deck_id", "questions"])
async def _add_questions(owner, args):
    _require(args, "deck_id", "questions")
    items = args["questions"]
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            raise HTTPException(400, "questions must be a JSON array of objects")
    if not isinstance(items, list):
        raise HTTPException(400, "questions must be an array")
    return await asyncio.to_thread(save_questions, owner, args["deck_id"], items,
                                   material_id=args.get("material_id"), origin="user")


@tool("update_question", "Edit a question's fields (text, context, options, correct_index, reference, topic, difficulty).",
      {"question_id": _s("Question id"), "question": _s("New question text"), "context": _s("Shared problem setup"),
       "options": {"type": "array", "items": {"type": "string"}, "description": "MCQ options"},
       "correct_index": _i("Correct option index"), "reference": _s("Model answer / solution"),
       "topic": _s("Topic label"), "difficulty": _s("easy | medium | hard", enum=["easy", "medium", "hard"])},
      ["question_id"])
async def _update_question(owner, args):
    _require(args, "question_id")
    sr = _sr()
    db = SessionLocal()
    try:
        row = sr._get_question(db, args["question_id"], owner)
        if args.get("question"):
            row.question = str(args["question"]).strip()
        if args.get("context") is not None:
            row.context = str(args["context"]).strip() or None
        if isinstance(args.get("options"), list):
            opts = [str(o).strip() for o in args["options"] if str(o).strip()]
            if row.qtype == "mcq" and len(opts) < 2:
                raise HTTPException(400, "MCQ needs at least 2 options")
            row.options = json.dumps(opts) if opts else None
            row.explanation = None
        if args.get("correct_index") is not None:
            opts = json.loads(row.options) if row.options else []
            ci = int(args["correct_index"])
            if not (0 <= ci < len(opts)):
                raise HTTPException(400, "correct_index out of range")
            row.correct_index = ci
            row.explanation = None
        if args.get("reference") is not None:
            row.reference = str(args["reference"])
            row.explanation = None
        if args.get("topic") is not None:
            row.topic = str(args["topic"]).strip() or None
        if args.get("difficulty") in ("easy", "medium", "hard"):
            row.difficulty = args["difficulty"]
        db.commit()
        return _q_row(row, full=True)
    finally:
        db.close()


@tool("delete_question", "Delete one question (and its attempt history).",
      {"question_id": _s("Question id")}, ["question_id"], destructive=True)
async def _delete_question(owner, args):
    _require(args, "question_id")
    db = SessionLocal()
    try:
        row = _sr()._get_question(db, args["question_id"], owner)
        db.query(StudyAttempt).filter(StudyAttempt.question_id == row.id).delete()
        db.delete(row)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@tool("set_question_suspended", "Suspend (drop from practice) or unsuspend a question.",
      {"question_id": _s("Question id"), "suspended": _b("true to suspend")}, ["question_id", "suspended"])
async def _set_question_suspended(owner, args):
    _require(args, "question_id")
    db = SessionLocal()
    try:
        row = _sr()._get_question(db, args["question_id"], owner)
        row.suspended = bool(args.get("suspended"))
        db.commit()
        return {"ok": True, "suspended": row.suspended}
    finally:
        db.close()


# ------------------------------------------------------------------ cards

@tool("list_cards", "List a subject's flashcards (front/back, state).",
      {"deck_id": _s("Subject id"), "query": _s("Substring filter"), "limit": _i("Max rows (default 50)")}, ["deck_id"])
async def _list_cards(owner, args):
    _require(args, "deck_id")
    limit = max(1, min(200, int(args.get("limit") or 50)))
    db = SessionLocal()
    try:
        _sr()._get_deck(db, args["deck_id"], owner)
        q = _owned(db.query(StudyCard).filter(StudyCard.deck_id == args["deck_id"]), StudyCard, owner)
        if args.get("query"):
            like = f"%{args['query']}%"
            q = q.filter(StudyCard.front.ilike(like) | StudyCard.back.ilike(like))
        total = q.count()
        rows = q.order_by(StudyCard.created_at.desc()).limit(limit).all()
        return {"total": total, "cards": [{"id": c.id, "front": c.front[:200], "back": c.back[:200],
                                          "state": c.state, "suspended": bool(c.suspended)} for c in rows]}
    finally:
        db.close()


@tool("add_cards", "Add flashcards to a subject. Each card: {front, back, notes?}. Keep cards atomic (one fact each).",
      {"deck_id": _s("Subject id"), "cards": {"type": "array", "items": {"type": "object"}, "description": "Cards"}},
      ["deck_id", "cards"])
async def _add_cards(owner, args):
    _require(args, "deck_id", "cards")
    cards = args["cards"]
    if isinstance(cards, str):
        cards = json.loads(cards)
    db = SessionLocal()
    try:
        deck = _sr()._get_deck(db, args["deck_id"], owner)
        now, ids = _now(), []
        for c in cards if isinstance(cards, list) else []:
            if not isinstance(c, dict):
                continue
            front, back = str(c.get("front") or "").strip(), str(c.get("back") or "").strip()
            if not front or not back:
                continue
            row = StudyCard(id=str(uuid.uuid4()), owner=owner, deck_id=deck.id, front=front, back=back,
                            notes=c.get("notes"), source="ai", state="new", due=now)
            db.add(row)
            ids.append(row.id)
        db.commit()
        return {"created": len(ids)}
    finally:
        db.close()


@tool("delete_card", "Delete one flashcard.", {"card_id": _s("Card id")}, ["card_id"], destructive=True)
async def _delete_card(owner, args):
    _require(args, "card_id")
    db = SessionLocal()
    try:
        card = _sr()._get_card(db, args["card_id"], owner)
        db.delete(card)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ------------------------------------------------------------------ exams / stats

@tool("list_exams", "List exams with dates, topics and whether a plan exists.")
async def _list_exams(owner, args):
    sr = _sr()
    db = SessionLocal()
    try:
        rows = _owned(db.query(StudyExam).filter(StudyExam.archived == False), StudyExam, owner) \
            .order_by(StudyExam.exam_date.asc()).all()  # noqa: E712
        out = []
        for e in rows:
            d = sr._exam_to_dict(e)
            out.append({k: d[k] for k in ("id", "title", "exam_date", "exam_format", "hours_per_week",
                                          "topics", "deck_id")} | {"has_plan": bool(d["plan"])})
        return {"exams": out}
    finally:
        db.close()


@tool("create_exam", "Create an exam (goal) with topics; then call generate_plan. Topics: [{name, importance 1-5, mastery 1-5}].",
      {"title": _s("Exam title"), "exam_date": _s("ISO date YYYY-MM-DD"),
       "topics": {"type": "array", "items": {"type": "object"}, "description": "Topics with importance/mastery"},
       "hours_per_week": {"type": "number", "description": "Study hours per week (default 7)"},
       "deck_id": _s("Linked subject id (plan blocks become practice buttons)"),
       "exam_format": _s("e.g. MCQ + problem set")}, ["title", "exam_date", "topics"])
async def _create_exam(owner, args):
    _require(args, "title", "exam_date", "topics")
    from datetime import date
    try:
        date.fromisoformat(str(args["exam_date"]))
    except ValueError:
        raise HTTPException(400, "exam_date must be YYYY-MM-DD")
    sr = _sr()
    topics = args["topics"] if isinstance(args["topics"], list) else json.loads(args["topics"])
    db = SessionLocal()
    try:
        exam = StudyExam(id=str(uuid.uuid4()), owner=owner, title=str(args["title"]).strip(),
                         exam_date=str(args["exam_date"]), exam_format=args.get("exam_format"),
                         hours_per_week=str(float(args.get("hours_per_week") or 7.0)),
                         topics=json.dumps(topics), deck_id=sr._vet_exam_deck(db, args.get("deck_id"), owner))
        db.add(exam)
        db.commit()
        return {"id": exam.id, "title": exam.title, "exam_date": exam.exam_date}
    finally:
        db.close()


@tool("generate_plan", "Generate (or regenerate) the spaced, interleaved study plan for an exam.",
      {"exam_id": _s("Exam id")}, ["exam_id"])
async def _generate_plan(owner, args):
    _require(args, "exam_id")
    from datetime import date
    from src.study_plan import generate_plan
    sr = _sr()
    db = SessionLocal()
    try:
        exam = sr._get_exam(db, args["exam_id"], owner)
        topics = json.loads(exam.topics) if exam.topics else []
        try:
            plan = generate_plan(date.fromisoformat(exam.exam_date), topics,
                                 hours_per_week=sr._flt(exam.hours_per_week, 7.0),
                                 rest_days=json.loads(exam.rest_days) if exam.rest_days else None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        exam.plan = json.dumps(plan)
        exam.done_blocks = json.dumps([])
        db.commit()
        meta = plan.get("meta", {})
        return {"ok": True, "days": len(plan.get("days", [])), "meta": meta}
    finally:
        db.close()


@tool("study_stats", "Today's dashboard (due counts, streak, focus minutes, exams) plus the last 30 answers/reviews with outcomes.")
async def _study_stats(owner, args):
    sr = _sr()
    ov = await asyncio.to_thread(sr.overview_payload, owner)
    hist = await asyncio.to_thread(sr.history_entries, owner, 30)
    entries = []
    for e in hist.get("entries", []):
        entries.append({"when": e.get("when"), "kind": e.get("kind"), "title": (e.get("title") or "")[:120],
                        "ok": (e.get("correct") is True) or ((e.get("score") or 0) >= 60) if e.get("kind") == "question"
                        else (e.get("rating") or 0) >= 3, "score": e.get("score"), "confidence": e.get("confidence")})
    return {"overview": {k: ov[k] for k in ("due_total", "q_due_total", "q_new_total", "today", "exams")},
            "subjects": ov.get("decks"), "recent": entries}


# ------------------------------------------------------------------ AI pipelines

@tool("extract_questions", "Run the AI question pipeline on a material: mode 'extract' pulls the real questions out of a practice/exam material (vision by default for PDFs); mode 'author' writes new questions from a theory material. Slow (minutes).",
      {"material_id": _s("Material id"), "mode": _s("extract | author", enum=["extract", "author"]),
       "count": _i("Target count for author mode (default 15)"),
       "types": {"type": "array", "items": {"type": "string", "enum": ["mcq", "open"]}, "description": "Restrict to types"}},
      ["material_id"])
async def _extract_questions(owner, args):
    _require(args, "material_id")
    res = await _sr().run_extraction(owner, args["material_id"], mode=args.get("mode") or "extract",
                                     types=args.get("types"), count=int(args.get("count") or 15))
    sample = [{"number": q.get("number"), "qtype": q.get("qtype"), "question": (q.get("question") or "")[:140]}
              for q in res.get("questions", [])[:8]]
    return {k: res.get(k) for k in ("created", "duplicates", "chunks", "chunk_errors", "vision", "coverage", "pages")} \
        | {"sample": sample}


@tool("transcribe_material", "Vision OCR: transcribe a scanned / formula-image PDF material into text so notes, search and text extraction work on it. Slow.",
      {"material_id": _s("Material id")}, ["material_id"])
async def _transcribe_material(owner, args):
    _require(args, "material_id")
    return await _sr().run_transcribe_material(owner, args["material_id"])


@tool("generate_notes", "Write (or rewrite) AI study notes for a material (Markdown; key concepts, formulas, methods, pitfalls, figures).",
      {"material_id": _s("Material id")}, ["material_id"])
async def _generate_notes(owner, args):
    _require(args, "material_id")
    res = await _sr().run_generate_notes(owner, args["material_id"])
    summary = res.get("summary") or ""
    return {"chars": len(summary), "has_figures": res.get("has_figures"), "preview": summary[:600]}


@tool("get_notes", "Read a material's AI study notes (paged, 6000 chars per call).",
      {"material_id": _s("Material id"), "offset": _i("Character offset")}, ["material_id"])
async def _get_notes(owner, args):
    _require(args, "material_id")
    db = SessionLocal()
    try:
        m = _sr()._get_material(db, args["material_id"], owner)
        text, name = m.summary or "", m.name
    finally:
        db.close()
    off = max(0, int(args.get("offset") or 0))
    nxt = off + MATERIAL_PAGE_CHARS
    return {"name": name, "total_chars": len(text), "offset": off, "text": text[off:nxt],
            "next_offset": nxt if nxt < len(text) else None,
            "note": None if text else "No notes yet — call generate_notes."}


@tool("generate_overview", "Write (or rewrite) the subject overview that ties its chapters together.",
      {"deck_id": _s("Subject id")}, ["deck_id"])
async def _generate_overview(owner, args):
    _require(args, "deck_id")
    res = await _sr().run_generate_overview(owner, args["deck_id"])
    ov = res.get("overview") or ""
    return {"chars": len(ov), "preview": ov[:600]}


@tool("maintain_bank", "Question-bank maintenance: dedup (remove duplicate questions), audit (suspend 'questions' that state their own answer), link_parts (group multi-part problems so practice shows earlier parts), backfill_context (recover shared problem setups).",
      {"action": _s("dedup | audit | link_parts | backfill_context", enum=["dedup", "audit", "link_parts", "backfill_context"]),
       "deck_id": _s("Subject id (required for link_parts)")}, ["action"])
async def _maintain_bank(owner, args):
    _require(args, "action")
    sr = _sr()
    action = args["action"]
    if action == "dedup":
        return await asyncio.to_thread(sr.run_dedup, owner)
    if action == "audit":
        return await sr.run_audit_questions(owner)
    if action == "backfill_context":
        return await sr.run_backfill_context(owner)
    if action == "link_parts":
        _require(args, "deck_id")
        db = SessionLocal()
        try:
            sr._get_deck(db, args["deck_id"], owner)
        finally:
            db.close()
        return await sr._link_deck_parts(owner, args["deck_id"])
    raise HTTPException(400, "unknown action")


# ------------------------------------------------------------------ code tools (admin, opt-in)

APP_MAP = """Odysseus Study module (FastAPI + SQLAlchemy backend, vanilla-JS ES-module frontend):
- routes/study_routes.py: /api/study/* — decks (subjects), cards + FSRS review queue, materials (upload/paste, category theory|exam, notes, transcribe, extract), question bank, practice queue + attempts (AI-graded open answers, hints, explain, explain-further with material citations, locate/consult), exams + deterministic plans, focus timer, overview/stats/history, bank maintenance (dedup/audit/link-parts/backfill-context). Heavy logic lives in module-level run_*/ *_payload functions.
- routes/study_agent_routes.py + src/study_agent.py: this agent (threads, SSE chat, tools).
- src/study_ai.py: prompts + pure helpers (JSON repair, question normalization, dedupe keys, discovery manifests, FSRS rating mapping). src/study_vision.py: PDF page rendering for vision models. src/fsrs.py: FSRS-4.5 scheduler. src/study_plan.py: plan generator.
- core/database.py: tables study_decks, study_cards, study_reviews, study_materials, study_questions, study_attempts, study_exams, study_focus_sessions, study_agent_threads, study_agent_messages.
- static/js/study.js: the Study pane (tabs Today, Subjects, Cards, Practice, Plan, Focus, History, Agent); static/js/studyAgent.js: the Agent tab. Styles are injected by study.js (injectStyles). Markdown+KaTeX via static/js/markdown.js mdToHtml.
- Tests: tests/test_study_*.py (pure helpers, plan generator, agent tools, practice filters).
Conventions: owner-scoped queries (`if user is not None: filter(owner == user)`), SessionLocal() per request in try/finally, HTTPException for user errors, LLM calls via _llm_json/_llm_text (text model) and _llm_json_vision (vision), extraction with thinking off + 16k completion budget."""


def _how_changes_apply() -> str:
    if running_in_docker():
        return ("Running in Docker: the code root is the container's copy of the app. JS/CSS edits are "
                "served immediately (hard-refresh the browser); Python edits need a server restart; ALL edits "
                "are lost when the image is rebuilt unless the repo is bind-mounted (see docker-compose.yml "
                "ODYSSEUS_CODE_DIR note). Tell the user this whenever you change code.")
    return ("Running natively: the code root is the live checkout. JS/CSS edits apply on browser refresh; "
            "Python edits need the server restarted (uvicorn without --reload). Tell the user.")


@tool("app_info", "Architecture map of the Study app, the code root the code tools are confined to, and how code changes take effect.", code=True)
async def _app_info(owner, args):
    return {"code_root": code_root(), "runtime": "docker" if running_in_docker() else "native",
            "how_changes_apply": _how_changes_apply(), "app_map": APP_MAP}


async def _run_code_tool(name: str, owner, args: Dict) -> Dict:
    """Bridge to the app's confined file/shell tools with the workspace bound
    to the code root (path confinement + sensitive-file deny list apply)."""
    from src.tool_execution import execute_tool_block, format_tool_result
    from src.tool_schemas import function_call_to_tool_block
    block = function_call_to_tool_block(name, json.dumps(args or {}))
    if block is None:
        raise HTTPException(400, f"could not build a {name} call from the given arguments")
    desc, result = await execute_tool_block(block, owner=owner, workspace=code_root())
    text = format_tool_result(desc, result or {})
    ok = (result or {}).get("exit_code", 0) in (0, None) and "error" not in (result or {})
    return {"ok": ok, "text": text}


def _code_tool(name: str, description: str, properties: Dict, required: List[str]):
    async def handler(owner, args, _name=name):
        return await _run_code_tool(_name, owner, args)
    TOOLS[name] = ToolSpec(name=name, description=description, handler=handler,
                           properties=properties, required=required, code=True,
                           destructive=name in ("write_file", "edit_file", "bash"))


_code_tool("read_file", "Read a file under the code root (optionally a line range).",
           {"path": _s("Path (relative to the code root or absolute inside it)"),
            "offset": _i("1-based first line"), "limit": _i("Max lines")}, ["path"])
_code_tool("write_file", "Create or overwrite a file under the code root.",
           {"path": _s("Path"), "content": _s("Full file content")}, ["path", "content"])
_code_tool("edit_file", "Exact-string edit of a file under the code root (old_string must be unique unless replace_all).",
           {"path": _s("Path"), "old_string": _s("Exact text to replace"), "new_string": _s("Replacement"),
            "replace_all": _b("Replace every occurrence")}, ["path", "old_string", "new_string"])
_code_tool("grep", "Regex search across the code root (file:line:match).",
           {"pattern": _s("Regular expression"), "path": _s("Subdirectory or file"),
            "glob": _s("Only files matching, e.g. *.py"), "ignore_case": _b("Case-insensitive"),
            "max_results": _i("Max matches")}, ["pattern"])
_code_tool("glob", "Find files by glob pattern under the code root.",
           {"pattern": _s("e.g. **/*.js"), "path": _s("Base directory")}, ["pattern"])
_code_tool("ls", "List a directory under the code root.", {"path": _s("Directory")}, [])
_code_tool("bash", "Run a shell command with the code root as working directory (tests, git status, node --check ...). Not sandboxed; prefer the file tools for edits.",
           {"command": _s("Shell command")}, ["command"])


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

ROLE = """You are the Study agent inside Odysseus's Study pane — a study assistant that ACTS through tools and tutors from the learner's own materials.

Rules:
- Use tools for anything about the learner's data; never guess ids or contents. When the user doesn't name a subject, use the focused subject below.
- Tutoring: ground explanations in the materials (search_materials, then get_material by page); cite "material name, p.N". If nothing covers it, say so before answering from general knowledge. Prefer making the learner retrieve (ask a probing question) over handing out full solutions unless asked.
- Destructive tools (delete_subject, remove_material, delete_question, delete_card, write_file, edit_file, bash) need the user's explicit go-ahead in this conversation first; then pass confirm=true where required.
- Be concise: short answers, no restating tool output, Markdown with LaTeX ($...$) for math. Never paste whole materials into a reply.
- Theory materials should get authored questions (mode 'author'); practice/exam materials get extracted questions (mode 'extract').
- If native tool calls are impossible, emit exactly one fenced block ```tool_call\n{"name": "...", "arguments": {...}}\n``` and stop.
- Long tools (extract, transcribe, notes) can take minutes; call them once and report the result."""


def _live_state(owner, deck_id: Optional[str]) -> str:
    sr = _sr()
    db = SessionLocal()
    try:
        decks = _owned(db.query(StudyDeck).filter(StudyDeck.archived == False), StudyDeck, owner) \
            .order_by(StudyDeck.created_at.asc()).all()  # noqa: E712
        lines = []
        focus = None
        for d in decks:
            c = sr._deck_counts(db, owner, d)
            n_mat = _owned(db.query(StudyMaterial).filter(StudyMaterial.deck_id == d.id),
                           StudyMaterial, owner).count()
            lines.append(f"- {d.name} (id {d.id}): {n_mat} materials, {c['q_total']} questions "
                         f"({c['q_due']} due, {c['q_new']} new), {c['total']} cards ({c['due_count']} due)")
            if deck_id and d.id == deck_id:
                focus = d
        out = ["Subjects:"] + (lines or ["- (none yet)"])
        if focus:
            out.append(f"Focused subject: {focus.name} (id {focus.id}). Its materials:")
            for r in sr.material_rows_with_counts(db, focus.id, owner):
                flags = []
                if r.get("thin_text"):
                    flags.append("thin text layer — transcribe first")
                if r.get("has_summary"):
                    flags.append("has notes")
                out.append(f"  - {r['name']} (id {r['id']}, {r['category']}, {r['kind']}, "
                           f"{(r['char_count'] or 0) // 1000}k chars, {r['question_count']} questions"
                           + (f"; {'; '.join(flags)}" if flags else "") + ")")
        elif deck_id:
            out.append(f"(Focused subject id {deck_id} was not found — ask the user.)")
        return "\n".join(out)
    finally:
        db.close()


def build_system_prompt(owner, deck_id: Optional[str], allow_code: bool, model: str = "") -> str:
    parts = [ROLE, f"Today: {datetime.now().date().isoformat()}." + (f" Model: {model}." if model else "")]
    try:
        parts.append(_live_state(owner, deck_id))
    except Exception as e:  # never block the chat on a state read
        logger.warning("study agent: live state failed: %s", e)
        parts.append("Subjects: (could not be read — use list_subjects)")
    if allow_code:
        parts.append("Code tools are ENABLED. Code root: " + code_root() + ". " + _how_changes_apply()
                     + "\nApp map:\n" + APP_MAP)
    else:
        parts.append("Code tools are disabled for this chat (the user can enable 'Allow code changes').")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def create_thread(owner, deck_id: Optional[str] = None, title: Optional[str] = None) -> Dict:
    db = SessionLocal()
    try:
        t = StudyAgentThread(id=str(uuid.uuid4()), owner=owner, deck_id=deck_id or None, title=title)
        db.add(t)
        db.commit()
        return _thread_dict(t)
    finally:
        db.close()


def _thread_dict(t: StudyAgentThread) -> Dict:
    return {"id": t.id, "title": t.title or "New chat", "deck_id": t.deck_id,
            "updated_at": (t.updated_at or t.created_at).isoformat() if (t.updated_at or t.created_at) else None}


def list_threads(owner, limit: int = 50) -> List[Dict]:
    db = SessionLocal()
    try:
        rows = _owned(db.query(StudyAgentThread), StudyAgentThread, owner) \
            .order_by(StudyAgentThread.updated_at.desc()).limit(limit).all()
        return [_thread_dict(t) for t in rows]
    finally:
        db.close()


def get_thread(owner, thread_id: str) -> StudyAgentThread:
    db = SessionLocal()
    try:
        t = db.query(StudyAgentThread).filter(StudyAgentThread.id == thread_id).first()
        if not t or (owner is not None and t.owner != owner):
            raise HTTPException(404, "Thread not found")
        db.expunge(t)
        return t
    finally:
        db.close()


def delete_thread(owner, thread_id: str) -> None:
    db = SessionLocal()
    try:
        t = db.query(StudyAgentThread).filter(StudyAgentThread.id == thread_id).first()
        if not t or (owner is not None and t.owner != owner):
            raise HTTPException(404, "Thread not found")
        db.query(StudyAgentMessage).filter(StudyAgentMessage.thread_id == t.id).delete()
        db.delete(t)
        db.commit()
    finally:
        db.close()


def save_message(owner, thread_id: str, role: str, content: Optional[str], *,
                 tool_calls: Optional[List[Dict]] = None, tool_call_id: Optional[str] = None,
                 name: Optional[str] = None) -> str:
    db = SessionLocal()
    try:
        m = StudyAgentMessage(id=str(uuid.uuid4()), owner=owner, thread_id=thread_id, role=role,
                              content=content, tool_calls=json.dumps(tool_calls) if tool_calls else None,
                              tool_call_id=tool_call_id, name=name)
        db.add(m)
        t = db.query(StudyAgentThread).filter(StudyAgentThread.id == thread_id).first()
        if t:
            t.updated_at = _now()
            if role == "user" and not t.title and content:
                t.title = " ".join(content.split())[:60]
        db.commit()
        return m.id
    finally:
        db.close()


def _load_rows(owner, thread_id: str, limit: Optional[int] = None) -> List[StudyAgentMessage]:
    db = SessionLocal()
    try:
        q = _owned(db.query(StudyAgentMessage).filter(StudyAgentMessage.thread_id == thread_id),
                   StudyAgentMessage, owner).order_by(StudyAgentMessage.created_at.asc(),
                                                      StudyAgentMessage.id.asc())
        rows = q.all()
        if limit:
            rows = rows[-limit:]
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()


def thread_messages_for_ui(owner, thread_id: str) -> List[Dict]:
    """Messages in the shape the Agent tab renders (tool results folded in)."""
    get_thread(owner, thread_id)
    out: List[Dict] = []
    for r in _load_rows(owner, thread_id):
        if r.role == "tool":
            out.append({"role": "tool", "name": r.name, "output": _clip(r.content or "", TOOL_RESULT_UI_CHARS),
                        "tool_call_id": r.tool_call_id, "when": r.created_at.isoformat() if r.created_at else None})
            continue
        item = {"role": r.role, "content": r.content or "", "when": r.created_at.isoformat() if r.created_at else None}
        if r.tool_calls:
            try:
                item["tool_calls"] = [{"id": c.get("id"), "name": c.get("name"), "args": c.get("arguments")}
                                      for c in json.loads(r.tool_calls)]
            except ValueError:
                pass
        out.append(item)
    return out


def rows_to_llm_messages(rows: List[StudyAgentMessage]) -> List[Dict]:
    msgs: List[Dict] = []
    for r in rows:
        if r.role == "tool":
            msgs.append({"role": "tool", "tool_call_id": r.tool_call_id or "", "content": r.content or ""})
        elif r.role == "assistant":
            m: Dict = {"role": "assistant", "content": r.content if (r.content or "").strip() else None}
            if r.tool_calls:
                try:
                    m["tool_calls"] = [{"id": c.get("id"), "type": "function",
                                        "function": {"name": c.get("name"), "arguments": c.get("arguments") or "{}"}}
                                       for c in json.loads(r.tool_calls)]
                except ValueError:
                    pass
            if m["content"] is None and not m.get("tool_calls"):
                continue
            msgs.append(m)
        else:
            msgs.append({"role": "user", "content": r.content or ""})
    return msgs


def trim_history(msgs: List[Dict], *, keep_rounds: int = 2,
                 old_chars: int = OLD_TOOL_RESULT_CHARS) -> List[Dict]:
    """Shrink tool results that are older than the last ``keep_rounds`` tool
    rounds (they served their purpose; the model only needs the gist)."""
    idx = [i for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls")]
    cutoff = idx[-keep_rounds] if len(idx) >= keep_rounds else (idx[0] if idx else None)
    if cutoff is None:
        return msgs
    out = []
    for i, m in enumerate(msgs):
        if m.get("role") == "tool" and i < cutoff and len(m.get("content") or "") > old_chars:
            m = {**m, "content": (m.get("content") or "")[:old_chars] + " ...[older tool output trimmed]"}
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# Tool-call parsing + dispatch
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```tool_call\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_XML_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)


def parse_fallback_tool_calls(text: str) -> List[Dict]:
    """Tool calls written as text by models that can't emit native ones:
    fenced ```tool_call {json}``` or <tool_call>{json}</tool_call>. Only
    registered tool names are accepted."""
    calls: List[Dict] = []
    for m in list(_FENCE_RE.finditer(text or "")) + list(_XML_RE.finditer(text or "")):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool")
        args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name in TOOLS and isinstance(args, dict):
            calls.append({"id": f"fb_{len(calls) + 1}", "name": name, "arguments": json.dumps(args)})
    return calls


def strip_fallback_blocks(text: str) -> str:
    return _XML_RE.sub("", _FENCE_RE.sub("", text or "")).strip()


def _parse_args(raw) -> Dict:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


async def dispatch_tool(name: str, owner, args: Dict, *, allow_code: bool = False) -> Dict:
    """Run one tool. Returns {"ok": bool, "result": ...} / {"ok": False, "error": ...}."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"ok": False, "error": f"unknown tool '{name}'"}
    if spec.code:
        from src.tool_security import owner_is_admin_or_single_user
        if not allow_code:
            return {"ok": False, "error": "code tools are disabled for this chat"}
        if not owner_is_admin_or_single_user(owner):
            return {"ok": False, "error": "code tools require an admin user"}
    try:
        result = await spec.handler(owner, args or {})
    except HTTPException as e:
        return {"ok": False, "error": str(e.detail)}
    except asyncio.CancelledError:
        raise
    except Exception as e:  # tool bugs must not kill the chat
        logger.exception("study agent tool %s failed", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if isinstance(result, dict) and "ok" in result and "text" in result and spec.code:
        return {"ok": bool(result["ok"]), "result": result["text"]}
    return {"ok": True, "result": result}


def result_text(res: Dict, limit: int) -> str:
    if not res.get("ok"):
        return _clip(f"ERROR: {res.get('error')}", limit)
    r = res.get("result")
    return _clip(r if isinstance(r, str) else _compact(r), limit)


def _args_preview(args: Dict, limit: int = 160) -> str:
    return _clip(_compact(args), limit)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def _sse(obj: Dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


DONE = "data: [DONE]\n\n"


def _iter_sse_events(chunk: str):
    """Yield (event, payload) pairs from one stream_llm chunk."""
    event = "message"
    for line in chunk.split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                yield "done", None
                continue
            try:
                yield event, json.loads(data)
            except ValueError:
                continue


async def run_study_agent(owner, thread_id: str, user_text: str, *, deck_id: Optional[str] = None,
                          allow_code: bool = False) -> AsyncGenerator[str, None]:
    """Stream one user turn: persist it, run up to MAX_ROUNDS model/tool
    rounds, persist every assistant/tool message, and yield SSE events."""
    from src.llm_core import stream_llm
    from src.tool_security import owner_is_admin_or_single_user

    sr = _sr()
    # Persist the user's turn first so the thread reflects what they typed even
    # when no model is configured yet (the error below is then the reply).
    save_message(owner, thread_id, "user", user_text)
    try:
        url, model, headers = sr._resolve_study_model(owner, prefer_text=True)
    except HTTPException as e:
        yield _sse({"type": "error", "message": str(e.detail)})
        yield DONE
        return

    include_code = bool(allow_code) and owner_is_admin_or_single_user(owner)
    schemas = tool_schemas(include_code)
    rows = _load_rows(owner, thread_id, HISTORY_MESSAGES)
    messages = [{"role": "system", "content": build_system_prompt(owner, deck_id, include_code, model)}]
    messages += trim_history(rows_to_llm_messages(rows))
    yield _sse({"type": "model_info", "model": model, "code_tools": include_code})

    for round_num in range(MAX_ROUNDS):
        text_parts: List[str] = []
        native_calls: List[Dict] = []
        stream_error: Optional[str] = None
        try:
            async for chunk in stream_llm(url, model, messages, temperature=0.2,
                                          max_tokens=REPLY_MAX_TOKENS, headers=headers,
                                          timeout=600, tools=schemas or None):
                for event, payload in _iter_sse_events(chunk):
                    if event == "error":
                        stream_error = (payload or {}).get("text") or (payload or {}).get("error") or "model error"
                    elif event == "done" or payload is None:
                        continue
                    elif "delta" in payload:
                        if payload.get("thinking"):
                            yield _sse({"delta": payload["delta"], "thinking": True})
                        else:
                            text_parts.append(payload["delta"])
                            yield _sse({"delta": payload["delta"]})
                    elif payload.get("type") == "tool_calls":
                        native_calls = payload.get("calls") or []
        except asyncio.CancelledError:
            raise
        except Exception as e:
            stream_error = f"{type(e).__name__}: {e}"

        text = "".join(text_parts)
        if stream_error:
            if text.strip():
                save_message(owner, thread_id, "assistant", text)
            yield _sse({"type": "error", "message": stream_error})
            break

        calls = native_calls
        if not calls:
            calls = parse_fallback_tool_calls(text)
            if calls:
                text = strip_fallback_blocks(text)

        if not calls:
            save_message(owner, thread_id, "assistant", text)
            break

        # Persist + append the assistant turn that requested the tools.
        stored_calls = [{"id": c.get("id") or f"call_{round_num}_{j}", "name": c.get("name", ""),
                         "arguments": c.get("arguments") if isinstance(c.get("arguments"), str)
                         else json.dumps(c.get("arguments") or {})}
                        for j, c in enumerate(calls)]
        save_message(owner, thread_id, "assistant", text or None, tool_calls=stored_calls)
        messages.append({"role": "assistant", "content": text if text.strip() else None,
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                                        for c in stored_calls]})

        for c in stored_calls:
            args = _parse_args(c["arguments"])
            yield _sse({"type": "tool_start", "id": c["id"], "name": c["name"], "args_preview": _args_preview(args)})
            res = await dispatch_tool(c["name"], owner, args, allow_code=include_code)
            model_text = result_text(res, TOOL_RESULT_MODEL_CHARS)
            ui_text = result_text(res, TOOL_RESULT_UI_CHARS)
            save_message(owner, thread_id, "tool", ui_text, tool_call_id=c["id"], name=c["name"])
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": model_text})
            yield _sse({"type": "tool_output", "id": c["id"], "name": c["name"], "ok": bool(res.get("ok")),
                        "output": ui_text})
        yield _sse({"type": "agent_step", "round": round_num + 1})
    else:
        yield _sse({"type": "error", "message": f"Stopped after {MAX_ROUNDS} tool rounds."})

    yield DONE
