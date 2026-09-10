# routes/study_routes.py
"""Study module API — evidence-based studying built into Odysseus.

Endpoints:
  /api/study/overview              dashboard: due counts, streak, today's plan
  /api/study/decks ...             flashcard deck CRUD
  /api/study/decks/{id}/cards ...  card CRUD (single + bulk)
  /api/study/queue                 FSRS review queue (due + capped new)
  /api/study/cards/{id}/review     apply a rating (FSRS schedule + review log)
  /api/study/ai/generate-cards     LLM: source text -> proposed flashcards
  /api/study/exams ...             exam CRUD + deterministic plan generation
  /api/study/focus ...             focus timer sessions
  /api/study/stats                 review/focus history for charts

Design notes:
- Scheduling is FSRS-4.5 (src/fsrs.py); plans are deterministic
  (src/study_plan.py). The LLM is only used where judgment is needed
  (authoring cards, grading free recall) — never for scheduling.
- The AI endpoints reuse the user's configured Odysseus model via
  resolve_endpoint("utility") with fallback to "default", same as notes.
"""

import asyncio
import threading
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from core.database import (
    SessionLocal,
    StudyAttempt,
    StudyCard,
    StudyDeck,
    StudyExam,
    StudyFocusSession,
    StudyMaterial,
    StudyQuestion,
    StudyReview,
    StudyUserParams,
)
from src.study_vision import text_layer_is_thin
from src.study_ai import (
    should_use_vision,
    offset_manifest,
    ADD_CONTEXT_SYSTEM,
    ASK_COACH_SYSTEM,
    ASK_ELABORATE_SYSTEM,
    ASK_TUTOR_SYSTEM,
    AUTHOR_QUESTIONS_SYSTEM,
    DISCOVER_QUESTIONS_SYSTEM,
    EXPLAIN_FURTHER_SYSTEM,
    context_is_redundant,
    repair_markdown_tables,
    EXPLAIN_SYSTEM,
    EXTRACT_QUESTIONS_SYSTEM,
    FIGURE_CAPTION_SYSTEM,
    GRADE_OPEN_SYSTEM,
    HINT_SYSTEM,
    LINK_PARTS_SYSTEM,
    LOCATE_MATERIAL_SYSTEM,
    MATERIAL_CATEGORIES,
    REFORMAT_SYSTEM,
    REPAIR_JSON_SYSTEM,
    SOLUTION_AUDIT_SYSTEM,
    STUDY_NOTES_SYSTEM,
    SUBJECT_OVERVIEW_SYSTEM,
    TRANSCRIBE_SYSTEM,
    chunk_material,
    classify_material,
    canonical_qnum,
    confidence_to_numeric,
    dedupe_questions,
    missing_question_numbers,
    normalize_questions,
    parse_answer_key_pages,
    parse_llm_json,
    parse_question_manifest,
    prereqs_from_groups,
    question_is_conclusion,
    question_key,
    rating_from_outcome,
)
from src.auth_helpers import get_current_user
from src.rate_limiter import RateLimiter
from src import fsrs
from src import fsrs_optimizer
from src import study_service

# The Study agent (src/study_agent.py) reaches the ownership getters through
# routes.study_routes, which re-exports this module. phase0 renamed them into
# src/study_service without the underscore; keep the original names bound here
# so the agent and the older tests keep resolving them.
_get_deck = study_service.get_deck
_get_card = study_service.get_card
_get_exam = study_service.get_exam
_get_material = study_service.get_material
_get_question = study_service.get_question
from src.study_source import build_original_question_link, infer_source_page
from src.study_plan import generate_plan, compute_mastery_scores, migrate_done_blocks, _semantic_interleave  # noqa: F401
from src.study_stats import (
    confidence_value,
    get_stats as _get_stats,
    get_calibration_curve,
    get_weak_question_signals,
    get_question_stability_signal,
    adaptive_question_priority as _adaptive_question_priority,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class DeckCreate(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    new_per_day: int = 15
    retention: float = 0.9


class DeckUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    new_per_day: Optional[int] = None
    retention: Optional[float] = None
    archived: Optional[bool] = None


class CardIn(BaseModel):
    front: str
    back: str
    notes: Optional[str] = None
    tags: Optional[List[str]] = None


class CardsCreate(BaseModel):
    cards: List[CardIn]
    source: str = "user"


class CardUpdate(BaseModel):
    front: Optional[str] = None
    back: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    suspended: Optional[bool] = None
    deck_id: Optional[str] = None


class ReviewIn(BaseModel):
    rating: int  # 1=Again 2=Hard 3=Good 4=Easy
    duration_ms: Optional[int] = None
    idempotency_key: Optional[str] = None


class GenerateCardsIn(BaseModel):
    text: Optional[str] = None
    material_id: Optional[str] = None  # use a stored material as the source
    count: int = 12
    focus: Optional[str] = None  # optional steer, e.g. "formulas and definitions"


class QuizIn(BaseModel):
    deck_id: Optional[str] = None
    text: Optional[str] = None
    count: int = 6


class GradeIn(BaseModel):
    question: str
    reference: str
    answer: str


class ExamCreate(BaseModel):
    title: str
    exam_date: str  # ISO date
    exam_format: Optional[str] = None
    hours_per_week: float = 7.0
    rest_days: Optional[List[int]] = None
    topics: List[Dict[str, Any]] = []
    deck_id: Optional[str] = None   # subject to practise from


class ExamUpdate(BaseModel):
    title: Optional[str] = None
    exam_date: Optional[str] = None
    exam_format: Optional[str] = None
    hours_per_week: Optional[float] = None
    rest_days: Optional[List[int]] = None
    topics: Optional[List[Dict[str, Any]]] = None
    archived: Optional[bool] = None
    deck_id: Optional[str] = None


class ToggleBlockIn(BaseModel):
    key: str  # "YYYY-MM-DD:block_index"


class FocusStart(BaseModel):
    label: Optional[str] = None
    planned_min: int = 25
    deck_id: Optional[str] = None      # Phase 3.2: attribute to a subject
    exam_id: Optional[str] = None      # Phase 3.2: attribute to a plan block
    block_key: Optional[str] = None    # Phase 3.2: "{date}:{idx}" in done_blocks


class FocusFinish(BaseModel):
    actual_min: int
    completed: bool = True


class MaterialCreate(BaseModel):
    name: Optional[str] = None
    text: Optional[str] = None
    file_id: Optional[str] = None


class MaterialCategoryIn(BaseModel):
    category: str   # "theory" | "exam"


class ExtractIn(BaseModel):
    mode: str = "extract"          # "extract" (past papers) | "author" (from notes)
    count: int = 15                # target question count (authoring hint)
    types: Optional[List[str]] = None  # subset of ["mcq", "open"]
    vision: bool = False           # render PDF pages -> vision model (formula/scan PDFs)


class QuestionUpdate(BaseModel):
    question: Optional[str] = None
    options: Optional[List[str]] = None
    correct_index: Optional[int] = None
    reference: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    suspended: Optional[bool] = None


class AttemptIn(BaseModel):
    choice_index: Optional[int] = None   # mcq
    answer: Optional[str] = None         # open, or mcq when typed_recall
    confidence: Optional[int | str] = None  # 0-100 numeric, or legacy label ("sure"/"unsure"/"guess")
    hints_used: int = 0
    duration_ms: Optional[int] = None
    idempotency_key: Optional[str] = None
    typed_recall: bool = False           # Phase 2.5: answer MCQ by free recall (uses open grading)
    wrong_mcq_gate: bool = False          # Phase 2.5: wrong MCQ requires re-engage before advancing


class HintIn(BaseModel):
    level: int = 1


class AskIn(BaseModel):
    message: str
    history: List[Dict] = []      # [{role: "student"|"ai", content: str}, ...]
    answered: bool = False        # have they submitted/checked yet?
    draft: Optional[str] = None   # their current/submitted answer text
    elaborate: bool = False       # opt-in: generate a why/how elaboration probe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _question_row_kwargs(item: Dict) -> Dict:
    """Chapter fields carried from an extracted item onto its StudyQuestion row.

    Kept in one place so extraction and the chapter backfill agree on the shape.
    Both come back None on a document with no chapter structure - an empty
    string would surface in the picker as an unnamed chapter."""
    try:
        idx = int(item.get("chapter_index") or 0) or None
    except (TypeError, ValueError):
        idx = None
    label = str(item.get("chapter") or "").strip() or None
    return {"chapter": label, "chapter_index": idx}


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt.tzinfo is None \
        else dt.astimezone(timezone.utc).isoformat()


def _flt(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _card_fsrs_dict(card: StudyCard) -> Dict:
    return {
        "state": card.state or "new",
        "stability": _flt(card.stability),
        "difficulty": _flt(card.difficulty),
        "last_review": card.last_review,
        "reps": card.reps or 0,
        "lapses": card.lapses or 0,
    }


def _card_to_dict(card: StudyCard, with_preview: bool = False) -> Dict:
    out = {
        "id": card.id,
        "deck_id": card.deck_id,
        "front": card.front,
        "back": card.back,
        "notes": card.notes,
        "tags": json.loads(card.tags) if card.tags else [],
        "suspended": bool(card.suspended),
        "source": card.source or "user",
        "state": card.state or "new",
        "stability": _flt(card.stability),
        "difficulty": _flt(card.difficulty),
        "due": _iso(card.due),
        "last_review": _iso(card.last_review),
        "reps": card.reps or 0,
        "lapses": card.lapses or 0,
    }
    if with_preview:
        out["preview"] = {str(k): v for k, v in
                          fsrs.preview_intervals(_card_fsrs_dict(card)).items()}
    return out


def _vet_exam_deck(db, deck_id, user):
    """Validate an exam's linked subject; "" / None clears the link."""
    deck_id = (deck_id or "").strip()
    if not deck_id:
        return None
    return study_service.get_deck(db, deck_id, user).id


def _exam_to_dict(exam: StudyExam) -> Dict:
    return {
        "id": exam.id,
        "title": exam.title,
        "exam_date": exam.exam_date,
        "exam_format": exam.exam_format,
        "hours_per_week": _flt(exam.hours_per_week, 7.0),
        "rest_days": json.loads(exam.rest_days) if exam.rest_days else [],
        "topics": json.loads(exam.topics) if exam.topics else [],
        "plan": json.loads(exam.plan) if exam.plan else None,
        "done_blocks": json.loads(exam.done_blocks) if exam.done_blocks else [],
        "deck_id": exam.deck_id,
        "archived": bool(exam.archived),
    }


def _focus_to_dict(s: StudyFocusSession) -> Dict:
    return {
        "id": s.id,
        "label": s.label,
        "planned_min": s.planned_min,
        "actual_min": s.actual_min,
        "started_at": _iso(s.started_at),
        "ended_at": _iso(s.ended_at),
        "completed": bool(s.completed),
        "deck_id": getattr(s, "deck_id", None),
        "exam_id": getattr(s, "exam_id", None),
        "block_key": getattr(s, "block_key", None),
    }


def _extract_json(raw: str):
    """Pull the first JSON value out of an LLM reply (tolerates fences/prose)."""
    if not isinstance(raw, str):
        raise ValueError("empty LLM reply")
    text = raw.strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for start_char in ("[", "{"):
        idx = text.find(start_char)
        while idx != -1:
            try:
                value, _ = decoder.raw_decode(text[idx:])
                return value
            except json.JSONDecodeError:
                idx = text.find(start_char, idx + 1)
    raise ValueError("no JSON found in LLM reply")


# Reasoning toggle sent with structured-JSON calls, in the two common shapes so
# whichever one the provider honors gets picked up (DeepSeek-style APIs read
# `thinking`; vLLM/Gemma-style chat templates read
# `chat_template_kwargs.enable_thinking`). Unknown fields are ignored upstream.
# Extraction is a transcription task: reasoning rumination only burns the
# completion budget and risks leaking commentary into the JSON output.
# (Framework borrowed from the Study Bench prototype.)
THINKING_OFF_BODY = {
    "thinking": {"type": "disabled"},
    "chat_template_kwargs": {"enable_thinking": False},
}

# Completion budget for extraction calls. Sized for reasoning models: even with
# the thinking-off hint, providers that ignore it (W&B Inference's Kimi-K2.6)
# emit thousands of hidden reasoning tokens BEFORE the JSON, and `content`
# comes back empty if the budget runs out mid-think. An upper bound, so it
# costs nothing for models that answer directly.
EXTRACTION_MAX_TOKENS = 16000

# Vision-transcription batch size: pages of rendered PDFs per model call.
# Kept small because each page comes back as full transcribed Markdown.
# (Lost in the routes/study split; restored from the pre-split monolith.)
TRANSCRIBE_PAGES_PER_CALL = 3


def _study_text_model(owner: Optional[str]) -> str:
    """Optional override model id for text-only Study calls (same endpoint)."""
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        return (get_user_setting("study_text_model", owner or "",
                                 settings.get("study_text_model", "")) or "").strip()
    except Exception:
        return ""


def study_model_info(owner: Optional[str]) -> dict:
    """Describe which model Study's AI passes will actually use.

    The model is not fixed: _resolve_study_model tries the Study endpoint, then
    utility, then default. Nothing surfaced which tier answered, so a pass that
    quietly fell back to utility looked like a hardcoded model the user could
    not choose. This reports the resolution, including its source, so the UI
    can say what will run.

    Deliberately returns no URL and no headers — headers carry API keys.
    """
    from src.endpoint_resolver import resolve_endpoint

    for source in ("study", "utility", "default"):
        try:
            url, model, _headers = resolve_endpoint(source, owner=owner or None)
        except Exception:
            continue
        if url and model:
            info = {"configured": True, "source": source, "model": model,
                    "text_model": None}
            if source == "study":
                info["text_model"] = _study_text_model(owner) or None
            return info
    return {"configured": False, "source": None, "model": None, "text_model": None}


def _resolve_study_model(owner: Optional[str], *, prefer_text: bool = False):
    """Resolve (url, model, headers) for a Study LLM call.

    When ``prefer_text`` and a ``study_text_model`` is configured, text-only
    calls run on that model on the SAME study endpoint — so a vision model can
    stay selected for image extraction while a faster/cheaper text model
    handles extraction-from-text, discovery, grading and hints. (Study Bench
    routes TEXT vs VISION the same way.)
    """
    from src.endpoint_resolver import resolve_endpoint

    url, model, headers = resolve_endpoint("study", owner=owner or None)
    if url and model and prefer_text:
        text_model = _study_text_model(owner)
        if text_model:
            model = text_model  # same endpoint/headers, different model id
    if not url:
        url, model, headers = resolve_endpoint("utility", owner=owner or None)
    if not url:
        url, model, headers = resolve_endpoint("default", owner=owner or None)
    if not url or not model:
        raise HTTPException(
            503, "No AI model configured. Add a model in Settings → Services first.")
    return url, model, headers


def _strip_think_safe(raw: str, *, prose: bool = False) -> str:
    try:
        from src.text_helpers import strip_think
        return strip_think(raw or "", prose=prose, prompt_echo=prose) or (raw or "")
    except Exception:
        return raw or ""


async def _repair_llm_json(url: str, model: str, headers, broken: str, err: str, *,
                           max_tokens: int, timeout: int):
    """One LLM pass that fixes malformed JSON (Study Bench's repair step).

    Models that wrap or slightly corrupt their JSON can usually fix it when
    shown the parser error. Runs with thinking off — syntax repair needs no
    reasoning. Raises ValueError if the repaired reply still doesn't parse.
    """
    from src.llm_core import llm_call_async

    raw = await llm_call_async(
        url=url, model=model,
        messages=[{"role": "system", "content": REPAIR_JSON_SYSTEM},
                  {"role": "user",
                   "content": f"Parser error: {err}\n\nBROKEN JSON:\n{broken[:80000]}"}],
        temperature=0.2, max_tokens=max_tokens,
        headers=headers, timeout=timeout, extra_body=THINKING_OFF_BODY,
    )
    return parse_llm_json(_strip_think_safe(raw))


async def _llm_json(owner: Optional[str], system: str, user: str, *,
                    temperature: float = 0.4, max_tokens: int = 3000,
                    timeout: int = 90, thinking_off: bool = False):
    """One-shot LLM call returning parsed JSON, using the user's model config.

    Text-only path: routes to the configured study_text_model when set.
    """
    from src.llm_core import llm_call_async

    url, model, headers = _resolve_study_model(owner, prefer_text=True)
    raw = await llm_call_async(
        url=url, model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature, max_tokens=max_tokens,
        headers=headers, timeout=timeout,
        extra_body=THINKING_OFF_BODY if thinking_off else None,
    )
    raw = _strip_think_safe(raw)
    parse_err = None
    try:
        return parse_llm_json(raw or "")
    except ValueError as e:
        parse_err = e

    text = (raw or "").strip()
    if not text:
        raise HTTPException(
            502, "The model returned an empty reply. Reasoning models can spend "
                 "the whole completion budget on hidden thinking before any "
                 "answer; retry, or pick a different Study model (the model "
                 "selector in the top bar).")

    # Salvage pass: ask the model to repair its own malformed JSON.
    if "{" in text or "[" in text:
        try:
            return await _repair_llm_json(url, model, headers, text, str(parse_err),
                                          max_tokens=max_tokens, timeout=timeout)
        except (ValueError, HTTPException) as e:
            detail = e.detail if isinstance(e, HTTPException) else e
            logger.warning("study: JSON repair pass failed: %s", detail)

    snippet = " ".join(text.split())[:160]
    logger.warning("study: LLM returned non-JSON reply: %.300s", raw)
    raise HTTPException(
        502, f'The model returned an unparseable reply (it said: "{snippet}...")'
             ". Try again or pick a stronger Study model (the model selector "
             "in the top bar).")


async def _llm_text(owner: Optional[str], system: str, user: str, *,
                    temperature: float = 0.3, max_tokens: int = 4000,
                    timeout: int = 120) -> str:
    """One-shot LLM call returning plain text (hints, explanations).

    The budget leaves room for reasoning models that think (hidden tokens)
    before the short visible answer. Text-only: routes to study_text_model
    when set.
    """
    from src.llm_core import llm_call_async

    url, model, headers = _resolve_study_model(owner, prefer_text=True)
    raw = await llm_call_async(
        url=url, model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature, max_tokens=max_tokens,
        headers=headers, timeout=timeout,
    )
    text = _strip_think_safe(raw, prose=True).strip()
    if not text:
        raise HTTPException(502, "The model returned an empty reply. Try again.")
    return text


def _material_to_dict(m: StudyMaterial, question_count: Optional[int] = None) -> Dict:
    """Serialize a material. ``question_count`` is the LIVE count of questions
    that still reference it (pass it from a grouped query); the stored column is
    only a fallback because it never decremented on delete."""
    pages = m.page_count or 0
    chars = m.char_count or 0
    return {
        "id": m.id, "deck_id": m.deck_id, "name": m.name, "kind": m.kind,
        "file_id": m.file_id, "char_count": chars,
        "page_count": pages or None,
        # Thin text layer (scanned / formula-image PDF): notes, consult and
        # text extraction cannot see the content until it is transcribed.
        "thin_text": bool(m.file_id and pages and text_layer_is_thin(chars, pages)),
        "question_count": (question_count if question_count is not None
                           else (m.question_count or 0)),
        "has_summary": bool(m.summary),
        "category": _material_category(m),
        "created_at": _iso(m.created_at),
    }


def _study_figures_dir(material_id: str) -> str:
    """Where extracted figures for a material live (inside the persisted
    uploads volume, one subdir per material)."""
    import os
    from src.constants import UPLOAD_DIR
    return os.path.join(UPLOAD_DIR, ".study_figures", os.path.basename(material_id))


def _resolve_locations(value: Dict, by_id: Dict[str, Dict]) -> List[Dict]:
    """Resolve a model reply's `locations` against the deck's materials.

    Returns deduped [{file_id, name, page, label, url}] — only locations whose
    material_id maps to a real file. `value` is {..., locations:[{material_id,
    page, label}]}; `by_id` maps material_id -> {name, file_id}.
    """
    locations = (value or {}).get("locations")
    if not isinstance(locations, list):
        return []
    seen = set()
    out: List[Dict] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        mat = by_id.get(str(loc.get("material_id") or ""))
        if not mat or not mat.get("file_id"):
            continue
        try:
            page = int(loc.get("page"))
            page = page if page >= 1 else None
        except (TypeError, ValueError):
            page = None
        key = (mat["file_id"], page)
        if key in seen:
            continue
        seen.add(key)
        name = mat.get("name") or "source"
        label = str(loc.get("label") or "").strip() or name
        url = f"/api/upload/{mat['file_id']}?inline=1" + (f"#page={page}" if page else "")
        out.append({"file_id": mat["file_id"], "name": name, "page": page,
                    "label": label, "url": url})
    return out


def _explain_further_markdown(value: Dict, by_id: Dict[str, Dict]) -> str:
    """Assemble the 'explain further' Markdown: the theory plus a 'Where to
    review' footer linking to each cited theory location across the subject's
    files (new browser tab, at the page when known) and the notes section."""
    explanation = str((value or {}).get("explanation") or "").strip()
    if not explanation:
        return ""
    section = str((value or {}).get("summary_section") or "").strip()

    bits = []
    for loc in _resolve_locations(value, by_id):
        label = loc["label"] + (f", p.{loc['page']}" if loc["page"] else "")
        # The frontend opens /api/upload links in a new tab (link post-processor).
        bits.append(f"[{label}]({loc['url']})")
    if section:
        bits.append(f"study notes → *{section}*")
    footer = ("\n\n---\n*Where to review:* " + " · ".join(bits)) if bits else ""
    return explanation + footer


def _material_category(m) -> str:
    """Effective category for a material: the stored value, or a filename guess
    for rows from before the column existed."""
    return m.category if m.category in MATERIAL_CATEGORIES else classify_material(m.name)


def _deck_material_context(db, deck_id: str, user, *, char_budget: int = 90000,
                           theory_only: bool = False):
    """Gather a deck's materials for an explain-further / locate search.

    Returns (blocks, by_id): `blocks` is a list of
    "=== MATERIAL <id>: <name> ===\\n<text>" strings (text carries the PDF
    [Page N] markers), `by_id` maps id -> {name, file_id, summary}.

    With ``theory_only``, materials categorized as exam/answer-key are excluded
    from the search corpus so citations point at the lecture/theory material —
    unless that would leave nothing, in which case all materials are used
    (graceful fallback)."""
    mq = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
    if user is not None:
        mq = mq.filter(StudyMaterial.owner == user)
    mats = mq.order_by(StudyMaterial.created_at.asc()).all()
    by_id = {m.id: {"name": m.name, "file_id": m.file_id, "summary": m.summary or ""}
             for m in mats}
    corpus = mats
    if theory_only:
        theory = [m for m in mats if _material_category(m) != "exam"]
        if theory:                      # keep all if the deck is exams-only
            corpus = theory
    blocks, budget = [], char_budget
    per = max(6000, char_budget // max(1, len(corpus))) if corpus else char_budget
    for m in corpus:
        body = (m.content or "").strip()
        if not body:
            continue
        chunk = f"=== MATERIAL {m.id}: {m.name} ===\n{body[:per]}"
        blocks.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return blocks, by_id


def _read_pref(owner, key: str) -> str:
    """Read a per-user preference (country, study_school) from the prefs store."""
    try:
        from routes.prefs_routes import _load_for_user
        return str((_load_for_user(owner) or {}).get(key) or "").strip()
    except Exception:
        return ""


def _compute_topic_affinity(topic_names: List[str]) -> Optional[Dict[str, Dict[str, float]]]:
    """Phase 4.1: compute pairwise topic similarity via embeddings.

    Returns a name→{name→cosine_similarity} dict, or None if embeddings are
    unavailable (the plan generator falls back to round-robin rotation).
    """
    if len(topic_names) < 2:
        return None
    try:
        from src.embeddings import get_embedding_client
        import numpy as np
        client = get_embedding_client()
        if client is None:
            return None
        vecs = client.encode(topic_names)  # (N, dim) float32
        if vecs is None or len(vecs) != len(topic_names):
            return None
        # cosine similarity (already normalized if normalize_embeddings=True)
        vecs = np.asarray(vecs)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = vecs / norms
        sim = normed @ normed.T  # (N, N)
        affinity: Dict[str, Dict[str, float]] = {}
        for i, a in enumerate(topic_names):
            affinity[a] = {}
            for j, b in enumerate(topic_names):
                affinity[a][b] = float(sim[i, j])
        return affinity
    except Exception:
        return None


def _web_source_links(sources, limit: int = 5) -> List[Dict]:
    """Normalize comprehensive_web_search sources into openable links."""
    out = []
    for s in (sources or []):
        if isinstance(s, str):
            url, title = s, s
        elif isinstance(s, dict):
            url = s.get("url") or s.get("link") or s.get("href") or ""
            title = s.get("title") or s.get("name") or url
        else:
            continue
        if not url:
            continue
        out.append({"name": title, "label": title, "url": url, "page": None,
                    "web": True})
        if len(out) >= limit:
            break
    return out


async def _web_theory(owner, concept_text: str, subject_name: str):
    """Last resort when no course material covers a question: find the theory on
    the web, localized to the user's school (else country, else English).
    Returns (markdown, [links]); ('', []) on failure. Best-effort."""
    country = _read_pref(owner, "country")
    school = _read_pref(owner, "study_school")
    loc_prompt = (
        f"Subject: {subject_name}\nA student needs the theory behind this:\n"
        f"{concept_text[:2000]}\n\n"
        f"Student's school: {school or 'unknown'}. Country: {country or 'unknown'}.\n"
        "Write a web search query that finds the lecture-level THEORY for this, "
        "in the language that school teaches it in (else the country's language, "
        "else English).\n"
        'Output ONLY JSON: {"query": "...", "language": "..."}')
    query, language = "", "English"
    try:
        q = await _llm_json(owner, "You craft one localized web search query. "
                            "Output only the JSON object.", loc_prompt,
                            temperature=0.2, max_tokens=400, timeout=60,
                            thinking_off=True)
        if isinstance(q, dict):
            query = str(q.get("query") or "").strip()
            language = str(q.get("language") or "English").strip() or "English"
    except HTTPException as e:
        logger.warning("study web theory: query gen failed: %s", e.detail)
    if not query:
        query = " ".join(x for x in [subject_name, concept_text[:120], school,
                                     country, "lecture notes theory"] if x)

    try:
        from src.search import comprehensive_web_search
        ctx, sources = await asyncio.to_thread(
            comprehensive_web_search, query, return_sources=True)
    except Exception as e:
        logger.warning("study web theory: search failed: %s", e)
        return "", []
    if not ctx:
        return "", []

    links = _web_source_links(sources)
    syn = (f"CONCEPT THE STUDENT IS STUCK ON:\n{concept_text[:2000]}\n\n"
           f"WEB SEARCH RESULTS (your only source):\n{ctx[:30000]}\n\n"
           f"Write the theory needed to understand this, in {language}, as "
           "Markdown with LaTeX for math, grounded ONLY in the results above. "
           "Begin with the exact line: _From the web — no matching course "
           "material._")
    try:
        md = await _llm_text(owner, "You explain course theory from web results, "
                             "faithfully and concisely.", syn, temperature=0.3,
                             max_tokens=4000, timeout=180)
    except HTTPException as e:
        logger.warning("study web theory: synthesis failed: %s", e.detail)
        md = ""
    return md, links


async def _append_web_theory(owner, base_md: str, concept: str,
                             subject_name: str) -> str:
    """Append a web-sourced theory section + source links to an explanation when
    no course material covered it. Returns base_md unchanged on failure."""
    web_md, links = await _web_theory(owner, concept, subject_name)
    if not (web_md or links):
        return base_md
    parts = [base_md or ""]
    if web_md:
        parts.append(web_md)
    if links:
        parts.append("*Web sources:* " + " · ".join(
            f"[{(l['name'] or 'source')[:60]}]({l['url']})" for l in links))
    return "\n\n".join(p for p in parts if p).strip()


_MATH_HINT_RE = re.compile(r"[\^_=]|\*|\\|\d\s*[+\-*/]\s*\d|\b[a-zA-Z]_[a-zA-Z0-9]")


def _needs_reformat(*texts) -> bool:
    """True if any text has no LaTeX ($) yet looks like it contains math/markup
    that would render badly as plain markdown (g_y, 2*3, x^2, =)."""
    joined = " ".join(t for t in texts if t)
    if "$" in joined:
        return False
    return bool(_MATH_HINT_RE.search(joined))


async def _reformat_items(owner, items: List[Dict]) -> Dict[str, Dict]:
    """Reformat a list of {id, <text fields>} dicts to LaTeX via the text model,
    in batches. Returns {id: reformatted_item}. Missing/failed items are simply
    absent (caller keeps the original)."""
    updates: Dict[str, Dict] = {}
    for i in range(0, len(items), 15):
        batch = items[i:i + 15]
        prompt = ("Reformat these items to LaTeX, preserving content exactly:\n\n"
                  + json.dumps(batch, ensure_ascii=False))
        try:
            value = await _llm_json(owner, REFORMAT_SYSTEM, prompt,
                                    temperature=0.1, max_tokens=EXTRACTION_MAX_TOKENS,
                                    timeout=300, thinking_off=True)
        except HTTPException as e:
            if e.status_code == 503:
                raise
            logger.warning("study reformat: batch %d failed: %s", i // 15, e.detail)
            continue
        arr = value if isinstance(value, list) else (
            value.get("items") if isinstance(value, dict) else None)
        for it in (arr or []):
            if isinstance(it, dict) and it.get("id"):
                updates[str(it["id"])] = it
    return updates


async def _backfill_context_items(owner, material_text: str,
                                  questions: List[Dict]) -> Dict[str, str]:
    """Recover the shared problem setup for split multi-part questions via the
    text model. `material_text` is the source material; `questions` are
    {id, number, question} dicts. Returns {id: context} for the questions that
    need it (others omitted). Best-effort: failed batches are skipped."""
    out: Dict[str, str] = {}
    mat = (material_text or "")[:50000]
    qtext = {str(q["id"]): q.get("question", "") for q in questions}
    for i in range(0, len(questions), 20):
        batch = questions[i:i + 20]
        prompt = ("MATERIAL:\n" + mat
                  + "\n\nQUESTIONS (id + text):\n"
                  + json.dumps(batch, ensure_ascii=False)
                  + "\n\nReturn the shared setup only for the questions that need it.")
        try:
            value = await _llm_json(owner, ADD_CONTEXT_SYSTEM, prompt,
                                    temperature=0.1, max_tokens=EXTRACTION_MAX_TOKENS,
                                    timeout=300, thinking_off=True)
        except HTTPException as e:
            if e.status_code == 503:
                raise
            logger.warning("study backfill-context: batch %d failed: %s", i // 20, e.detail)
            continue
        arr = value.get("items") if isinstance(value, dict) else (
            value if isinstance(value, list) else [])
        for it in (arr or []):
            if isinstance(it, dict) and it.get("id"):
                ctx = str(it.get("context") or "").strip()
                # Transposing a cross-tab out of a PDF, models keep both
                # dimension names in the header and write one label per row,
                # which slides every value a column left when rendered. Repair
                # before storing so the bad shape never reaches a student.
                ctx = repair_markdown_tables(ctx)
                # Drop context that just repeats the question (the model
                # sometimes adds setup to already self-contained questions).
                if ctx and not context_is_redundant(qtext.get(str(it["id"]), ""), ctx):
                    out[str(it["id"])] = ctx
    return out


async def _audit_solution_statements(owner, questions: List[Dict]) -> set:
    """Flag open questions that state their own answer/conclusion (worked-
    solution steps leaked into the bank). `questions` are {id, question} dicts.
    Returns the set of flagged ids. Best-effort: failed batches are skipped."""
    flagged: set = set()
    valid = {str(q["id"]) for q in questions}
    for i in range(0, len(questions), 25):
        batch = questions[i:i + 25]
        try:
            value = await _llm_json(owner, SOLUTION_AUDIT_SYSTEM,
                                    json.dumps(batch, ensure_ascii=False),
                                    temperature=0.0, max_tokens=4000,
                                    timeout=240, thinking_off=True)
        except HTTPException as e:
            if e.status_code == 503:
                raise
            logger.warning("study audit: batch %d failed: %s", i // 25, e.detail)
            continue
        ids = value.get("flag") if isinstance(value, dict) else (
            value if isinstance(value, list) else [])
        for qid in (ids or []):
            if str(qid) in valid:
                flagged.add(str(qid))
    return flagged


async def _link_deck_parts(owner, deck_id: str, only_material: Optional[str] = None) -> Dict:
    """Group a deck's questions into multi-part problems (per material) and store
    each part's prereq_ids = all earlier parts of its problem, so practice can
    show those earlier parts + the learner's answers as exam-style context.

    Re-groups from scratch (clears stale links). With ``only_material`` it
    processes just that material — used to auto-link right after an extraction.
    Returns {linked, analyzed}."""
    from collections import defaultdict
    db = SessionLocal()
    try:
        q = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck_id)
        if owner is not None:
            q = q.filter(StudyQuestion.owner == owner)
        if only_material is not None:
            q = q.filter(StudyQuestion.material_id == only_material)
        rows = q.order_by(StudyQuestion.created_at.asc()).all()
        by_mat = defaultdict(list)
        number_by_id: Dict[str, str] = {}
        question_by_id: Dict[str, StudyQuestion] = {}
        for r in rows:
            number_by_id[r.id] = r.number
            question_by_id[r.id] = r
            # Include the recovered setup: terse parts ("verify the objective is
            # differentiable") don't name their problem, so without their context
            # the grouper can't place them with the right problem.
            text = (r.question or "")[:600]
            ctx = (r.context or "").strip()
            if ctx:
                text += f"\n[shared setup: {ctx[:400]}]"
            by_mat[r.material_id or "none"].append(
                {"id": r.id, "number": r.number, "question": text})
    finally:
        db.close()

    results: Dict[str, List[str]] = {}
    analyzed_ids: List[str] = []
    for mat_id, items in by_mat.items():
        if len(items) < 2:                  # need siblings to form a problem
            continue
        analyzed_ids.extend(it["id"] for it in items)
        valid = {it["id"] for it in items}
        try:
            value = await _llm_json(owner, LINK_PARTS_SYSTEM,
                                    json.dumps(items, ensure_ascii=False),
                                    temperature=0.1, max_tokens=8000,
                                    timeout=240, thinking_off=True)
        except HTTPException as e:
            if e.status_code == 503:
                raise
            logger.warning("study link-parts: material %s failed: %s", mat_id, e.detail)
            continue
        groups = value.get("groups") if isinstance(value, dict) else (
            value if isinstance(value, list) else [])
        results.update(prereqs_from_groups(groups, valid_ids=valid,
                                           number_by_id=number_by_id))

    linked = 0
    db = SessionLocal()
    try:
        for r in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(analyzed_ids)).all()
                  if analyzed_ids else []):
            pre = [
                p for p in results.get(r.id, [])
                if (
                    p != r.id
                    and not _same_study_question(r, question_by_id.get(p))
                    and not _same_or_later_study_part(r, question_by_id.get(p))
                )
            ]
            r.prereq_ids = json.dumps(pre)   # also clears stale links when empty
            if pre:
                linked += 1
        db.commit()
    finally:
        db.close()
    return {"linked": linked, "analyzed": len(analyzed_ids)}


def _dedup_deck_questions(db, deck_id: str, owner) -> int:
    """Remove duplicate questions in a deck (same notation-insensitive
    question_key) — e.g. the same exam part extracted twice, once plain and once
    in LaTeX. Keeps the best copy of each cluster and deletes the rest; returns
    the number deleted. 'Best' = has a part number (canonical for ordering), then
    most attempts (preserve progress), then longest text."""
    from collections import defaultdict
    q = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck_id)
    if owner is not None:
        q = q.filter(StudyQuestion.owner == owner)
    rows = q.all()
    clusters: Dict[str, list] = defaultdict(list)
    for r in rows:
        clusters[question_key(r.question or "")].append(r)

    deleted = 0
    for key, group in clusters.items():
        if len(group) < 2:
            continue
        qids = [r.id for r in group]
        att_raw = db.query(StudyAttempt.question_id).filter(
            StudyAttempt.question_id.in_(qids)
        ).all()
        counts = {}
        for qid in att_raw:
            counts[qid[0]] = counts.get(qid[0], 0) + 1
        att = {r.id: counts.get(r.id, 0) for r in group}
        group.sort(key=lambda r: (1 if r.number else 0, att[r.id], len(r.question or "")),
                   reverse=True)
        for dup in group[1:]:                 # keep group[0], drop the rest
            db.delete(dup)
            deleted += 1
    if deleted:
        db.commit()
    return deleted


async def _build_figures_section(owner, material_id: str, file_id: str,
                                 pdf_path: str) -> str:
    """Extract raster figures from a PDF, caption/keep the substantive ones via
    a vision pass, and return a Markdown '## Key figures' section embedding them
    with source-page citations. Best-effort: returns '' on any shortfall."""
    from src.study_vision import extract_pdf_figures, figure_data_url

    try:
        figs = extract_pdf_figures(pdf_path, _study_figures_dir(material_id))
    except Exception as e:
        logger.warning("study notes: figure extraction failed: %s", e)
        return ""
    if not figs:
        return ""

    captions: Dict[int, str] = {}
    keep: set = set()
    value = None
    try:
        urls = [figure_data_url(f["path"]) for f in figs]
        value = await _llm_json_vision(
            owner, FIGURE_CAPTION_SYSTEM,
            f"Caption these {len(urls)} figures, in order.", urls,
            max_tokens=3000, timeout=180)
    except Exception as e:
        logger.warning("study notes: figure captioning failed: %s", e)
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict) or "idx" not in item:
                continue
            try:
                ix = int(item["idx"])
            except (TypeError, ValueError):
                continue
            captions[ix] = str(item.get("caption") or "").strip()
            if item.get("keep"):
                keep.add(ix)
        kept = [f for f in figs if f["idx"] in keep]
    else:
        # Captioning unavailable — keep what we extracted with generic captions.
        kept = figs

    if not kept:
        return ""
    lines = ["\n\n## Key figures\n"]
    for f in kept:
        cap = captions.get(f["idx"]) or f"Figure (p.{f['page']})"
        img_url = f"/api/study/figures/{material_id}/{f['idx']}"
        src_url = f"/api/upload/{file_id}?inline=1#page={f['page']}"
        lines.append(f"![{cap}]({img_url})\n\n*{cap} — [source: p.{f['page']}]({src_url})*\n")
    return "\n".join(lines)


def _question_fsrs_dict(q: StudyQuestion) -> Dict:
    return {
        "state": q.state or "new",
        "stability": _flt(q.stability),
        "difficulty": _flt(q.fsrs_difficulty),
        "last_review": q.last_review,
        "reps": q.reps or 0,
        "lapses": q.lapses or 0,
    }


def _question_to_dict(q: StudyQuestion, with_answer: bool = True,
                      original: Optional[Dict] = None) -> Dict:
    out = {
        "id": q.id, "deck_id": q.deck_id, "material_id": q.material_id,
        "qtype": q.qtype, "question": q.question,
        "context": q.context,
        "options": json.loads(q.options) if q.options else None,
        "topic": q.topic, "difficulty": q.difficulty,
        "origin": q.origin, "suspended": bool(q.suspended),
        "state": q.state or "new", "due": _iso(q.due),
        "reps": q.reps or 0, "lapses": q.lapses or 0,
        "number": q.number,
        "source_page": q.source_page,
        "original": original,
        "has_prereqs": bool(q.prereq_ids and q.prereq_ids != "[]"),
    }
    if with_answer:
        out["correct_index"] = q.correct_index
        out["reference"] = q.reference
        out["explanation"] = q.explanation
    return out


def _question_original_link(q: StudyQuestion,
                            material: Optional[StudyMaterial]) -> Optional[Dict]:
    if not material or not material.file_id or q.origin != "extracted":
        return None
    page = q.source_page
    if not page and material.content:
        page = infer_source_page(material.content, number=q.number, question=q.question)
    return build_original_question_link(material.file_id, page=page, name=material.name)


def _question_original_links(db, rows: List[StudyQuestion]) -> Dict[str, Optional[Dict]]:
    material_ids = sorted({r.material_id for r in rows if r.material_id})
    if not material_ids:
        return {}
    materials = {
        m.id: m for m in
        db.query(StudyMaterial).filter(StudyMaterial.id.in_(material_ids)).all()
    }
    return {r.id: _question_original_link(r, materials.get(r.material_id)) for r in rows}


def _same_study_question(a, b) -> bool:
    """True when two stored rows are duplicate copies of the same prompt."""
    ak = question_key(getattr(a, "question", "") or "")
    bk = question_key(getattr(b, "question", "") or "")
    return bool(ak and ak == bk)


def _study_part_order_key(q):
    label = getattr(q, "number", None)
    if not label:
        return None
    s = re.sub(r"[^0-9a-z]", "", str(label).strip().lower())
    m = re.match(r"(\d+)([a-z]*)", s)
    if not m:
        return None
    return (int(m.group(1)), m.group(2))


def _same_or_later_study_part(current, candidate) -> bool:
    current_key = _study_part_order_key(current)
    if current_key is None:
        return False
    candidate_key = _study_part_order_key(candidate)
    return candidate_key is None or candidate_key >= current_key


def _resolve_uploaded_file(file_id: str) -> str:
    """Find an uploaded file on disk. Uploads live in date-based subfolders
    (UPLOAD_DIR/YYYY/MM/DD/<id>), so check the index first, then the root,
    then walk — mirroring routes/upload_routes._resolve_upload_path."""
    import os
    from src.constants import UPLOAD_DIR
    root = os.path.realpath(UPLOAD_DIR)
    safe = os.path.basename(file_id or "")
    if not safe:
        raise HTTPException(404, "Uploaded file not found")

    def _confined(p: str) -> bool:
        return os.path.realpath(p).startswith(root + os.sep)

    # 1) uploads.json index has the exact stored path
    try:
        with open(os.path.join(UPLOAD_DIR, "uploads.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        info = index.get(safe) or next(
            (v for v in index.values() if v.get("id") == safe), None)
        stored = (info or {}).get("path")
        if stored and os.path.isfile(stored) and _confined(stored):
            return stored
    except Exception:
        pass

    # 2) direct join (legacy flat layout)
    direct = os.path.join(UPLOAD_DIR, safe)
    if os.path.isfile(direct) and _confined(direct):
        return direct

    # 3) walk the date-based tree
    for walk_root, _dirs, files in os.walk(UPLOAD_DIR, followlinks=False):
        if safe in files:
            candidate = os.path.join(walk_root, safe)
            if os.path.isfile(candidate) and _confined(candidate):
                return candidate
    raise HTTPException(404, "Uploaded file not found")


def _extract_file_text(file_id: str, owner) -> str:
    """Resolve an uploaded file and extract its text for question extraction."""
    import os
    path = _resolve_uploaded_file(file_id)
    ext = os.path.splitext(path)[1].lower()
    try:
        from src import document_processor as dp
        # max_chars=None: keep the FULL paper. The 15k cap on these extractors
        # exists to protect a chat context window; study materials must not be
        # silently truncated (extraction and notes need the whole document).
        if ext == ".pdf":
            text = dp._process_pdf(path, owner=owner, max_chars=None)
        else:
            try:
                from src.markitdown_runtime import is_markitdown_format
                office = is_markitdown_format(path)
            except Exception:
                office = False
            if office:
                text = dp._process_office_document(path, os.path.basename(path),
                                                   max_chars=None)
            else:
                text = dp._process_text_file(path, max_chars=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("study: file text extraction failed: %s", e)
        raise HTTPException(422, f"Could not extract text from this file: {e}")
    text = (text or "").strip()
    if len(text) < 30:
        raise HTTPException(422, "No usable text found in the file. If it is a "
                                 "scanned PDF, use vision extraction instead "
                                 "(Extract (vision) button) or paste the content as text.")
    return text


def _vision_candidates(owner) -> List:
    """(url, model, headers) candidates for image calls: the user's study
    model first (they may have picked a VLM), then the configured/auto
    vision model, then the vision fallback chain."""
    cands = []
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("study", owner=owner or None)
        if url and model:
            cands.append((url, model, headers))
    except Exception:
        pass
    try:
        from src.document_processor import _load_vl_settings, _resolve_vl_model
        settings = _load_vl_settings()
        url, model, headers = _resolve_vl_model(settings.get("vision_model", ""), owner=owner)
        if url and model:
            cands.append((url, model, headers))
    except Exception as e:
        logger.debug("study vision: vl model resolution failed: %s", e)
    try:
        from src.endpoint_resolver import resolve_vision_fallback_candidates
        cands.extend(c for c in resolve_vision_fallback_candidates(owner=owner)
                     if c and c[0] and c[1])
    except Exception:
        pass
    # dedupe, preserve order
    seen, out = set(), []
    for c in cands:
        key = (c[0], c[1])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


async def _llm_json_vision(owner, system: str, instruction: str,
                           image_urls: List[str], *, temperature: float = 0.2,
                           max_tokens: int = EXTRACTION_MAX_TOKENS,
                           timeout: int = 300):
    """One-shot vision call returning parsed JSON; iterates model candidates."""
    from src.llm_core import llm_call_async

    candidates = _vision_candidates(owner)
    if not candidates:
        raise HTTPException(503, "No vision-capable model available. Pick a vision "
                                 "model in the Study model selector, or configure "
                                 "one in Settings -> Vision.")
    content = [{"type": "text", "text": instruction}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in image_urls
    ]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": content}]
    last_detail = None
    for url, model, headers in candidates:
        try:
            raw = await llm_call_async(url=url, model=model, messages=messages,
                                       temperature=temperature, max_tokens=max_tokens,
                                       headers=headers, timeout=timeout,
                                       extra_body=THINKING_OFF_BODY)
            raw = _strip_think_safe(raw)
            try:
                return parse_llm_json(raw or "")
            except ValueError as parse_err:
                text = (raw or "").strip()
                if text and ("{" in text or "[" in text):
                    # Salvage pass: have the model repair its own JSON.
                    return await _repair_llm_json(url, model, headers, text,
                                                  str(parse_err),
                                                  max_tokens=max_tokens,
                                                  timeout=timeout)
                raise
        except ValueError:
            snippet = " ".join((raw or "").split())[:160]
            if snippet:
                last_detail = (f'Vision model {model} returned an unparseable '
                               f'reply (it said: "{snippet}...")')
            else:
                last_detail = (f"Vision model {model} returned an empty reply — "
                               "it may not support image input, or it spent the "
                               "whole completion budget on hidden reasoning.")
            logger.warning("study vision: %s", last_detail)
        except HTTPException as e:
            last_detail = f"Vision model {model} failed: {e.detail}"
            logger.warning("study vision: %s", last_detail)
        except Exception as e:
            last_detail = (f"Vision model {model} failed ({type(e).__name__}): {e}. "
                           "It may not support image input.")
            logger.warning("study vision: %s", last_detail)
    raise HTTPException(502, last_detail or "All vision model candidates failed.")


PAGES_PER_BATCH = 3


# A whole 40-page exam in one vision request blows most context
# windows, so discovery runs in batches this size and offsets each
# batch's page numbers back onto the document.
DISCOVERY_PAGES_PER_CALL = 12


async def _discover_questions_vision(owner, page_urls: List[str]) -> tuple:
    """Discovery pass: list every question label + the answer-key pages.

    Runs over the already-rendered pages in batches of DISCOVERY_PAGES_PER_CALL
    (a whole 40-page exam in one request blows most vision context windows);
    every batch's page numbers are shifted by its offset so the merged manifest
    covers the whole document and the coverage check sees all of it. Full
    render scale — low-res renders proved illegible to the vision model.
    Best-effort: a failed batch is skipped rather than blocking extraction.
    (Study Bench's manifest approach.)

    Returns (manifest, answer_key_pages).
    """
    manifest: List[Dict] = []
    key_pages: List[int] = []
    for start in range(0, len(page_urls), DISCOVERY_PAGES_PER_CALL):
        urls = page_urls[start:start + DISCOVERY_PAGES_PER_CALL]
        try:
            value = await _llm_json_vision(
                owner, DISCOVER_QUESTIONS_SYSTEM,
                f"List every explicit question in these {len(urls)} pages "
                f"(they are pages {start + 1}-{start + len(urls)} of the document; "
                f"number pages 1-{len(urls)} relative to this batch).",
                urls, max_tokens=8000, timeout=240)
        except HTTPException as e:
            logger.warning("study discovery: vision manifest batch %d failed: %s",
                           start // DISCOVERY_PAGES_PER_CALL + 1, e.detail)
            continue
        except Exception as e:
            logger.warning("study discovery: vision manifest batch %d failed: %s",
                           start // DISCOVERY_PAGES_PER_CALL + 1, e)
            continue
        manifest.extend(offset_manifest(parse_question_manifest(value), start))
        key_pages.extend(p + start for p in parse_answer_key_pages(value))
    # Dedupe manifest numbers across batches (a question spanning a batch
    # boundary can be listed twice); first occurrence wins.
    seen, merged = set(), []
    for m in manifest:
        key = canonical_qnum(m.get("number"))
        if key and key not in seen:
            seen.add(key)
            merged.append(m)
    return merged, sorted(set(key_pages))


async def _discover_questions_text(owner, content: str) -> List[Dict]:
    """Discovery pass over the material's text layer. Best-effort, [] on failure."""
    try:
        value = await _llm_json(
            owner, DISCOVER_QUESTIONS_SYSTEM,
            "List every explicit question in this material.\n\n--- MATERIAL ---\n"
            + content[:48000],
            temperature=0.1, max_tokens=8000, timeout=240, thinking_off=True)
        return parse_question_manifest(value)
    except HTTPException as e:
        logger.warning("study discovery: text manifest failed: %s", e.detail)
        return []


def _coverage_report(manifest: List[Dict], collected: List[Dict]) -> Optional[Dict]:
    """Compare extracted question numbers against the discovery manifest.

    Returns {"expected", "matched", "missing"} or None when no manifest. If
    the extractor didn't label its questions, coverage is unknowable — report
    expected count with matched=None instead of flagging everything missing.
    """
    if not manifest:
        return None
    extracted_nums = [q.get("number") for q in collected if q.get("number")]
    if collected and not extracted_nums:
        return {"expected": len(manifest), "matched": None, "missing": []}
    missing = missing_question_numbers(manifest, extracted_nums)
    return {"expected": len(manifest),
            "matched": len(manifest) - len(missing),
            "missing": [m["number"] for m in missing]}


def _manifest_page_map(manifest: List[Dict]) -> Dict[str, int]:
    return {canonical_qnum(m.get("number")): m.get("page")
            for m in (manifest or []) if canonical_qnum(m.get("number"))}


def _attach_source_pages(items: List[Dict], page_by_number: Dict[str, int],
                         *, fallback_page: Optional[int] = None) -> List[Dict]:
    for q in items:
        if q.get("source_page"):
            continue
        page = page_by_number.get(canonical_qnum(q.get("number"))) if q.get("number") else None
        if not page and fallback_page:
            page = fallback_page
        if page:
            q["source_page"] = page
    return items


async def _extract_questions_vision(owner, mode: str, types: List[str],
                                    pdf_path: str) -> tuple:
    """Render PDF pages and extract questions via a vision model.

    mode "extract" runs a discovery pass first that also flags answer-key /
    solution pages. Those pages are NOT mined for questions — they're attached
    to each extraction batch as reference-only context so the model can fill in
    correct answers and reference solutions without turning the worked
    solutions into questions. When the manifest shows questions the extraction
    missed, a targeted re-extraction pulls just those, by page.

    Returns (normalized_questions, raw_count, batches, errors, coverage).
    """
    from src.study_vision import render_pdf_pages, pages_to_data_urls

    try:
        page_urls = pages_to_data_urls(render_pdf_pages(pdf_path))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    # render_pdf_pages caps at MAX_PAGES; say so rather than silently reading
    # only the front of a long exam.
    from src.study_vision import MAX_PAGES, pdf_page_count
    total_pages = pdf_page_count(pdf_path) or len(page_urls)
    page_info = {"pages": len(page_urls), "total_pages": total_pages,
                 "truncated": total_pages > len(page_urls)}
    if page_info["truncated"]:
        logger.warning("study vision: %s has %d pages; only the first %d are read "
                       "(MAX_PAGES=%d)", pdf_path, total_pages, len(page_urls), MAX_PAGES)
    system = EXTRACT_QUESTIONS_SYSTEM if mode == "extract" else AUTHOR_QUESTIONS_SYSTEM
    type_note = ("Only produce questions of type: " + ", ".join(types) + ". ")         if len(types) == 1 else ""
    verb = ("Extract every practice question visible in these exam pages, "
            "transcribing all mathematics as LaTeX ($...$ inline, $$...$$ "
            "display) faithfully to the original."
            if mode == "extract" else
            "Write practice questions from the content visible in these pages.")

    manifest, answer_key_pages = (
        await _discover_questions_vision(owner, page_urls)
        if mode == "extract" else ([], []))
    page_by_number = _manifest_page_map(manifest)
    n_pages = len(page_urls)
    key_set = {p for p in answer_key_pages if 1 <= p <= n_pages}

    # Question pages = everything not flagged as an answer key. If discovery
    # called the WHOLE document an answer key (false positive), ignore it
    # rather than extract nothing.
    q_page_nums = [n for n in range(1, n_pages + 1) if n not in key_set]
    if not q_page_nums:
        q_page_nums = list(range(1, n_pages + 1))
        key_set = set()
    key_urls = [page_urls[p - 1] for p in sorted(key_set)]
    key_note = (" Some attached images are ANSWER-KEY / solution pages; use them "
                "ONLY to fill in correct answers and reference solutions — do NOT "
                "create questions from them." if key_urls else "")

    # Batch the question pages, tracking original page numbers per batch.
    batches = [q_page_nums[i:i + PAGES_PER_BATCH]
               for i in range(0, len(q_page_nums), PAGES_PER_BATCH)]

    collected: List[Dict] = []
    raw_count = 0
    errors = 0
    for bi, nums in enumerate(batches):
        urls = [page_urls[n - 1] for n in nums] + key_urls
        instruction = (f"{verb} {type_note}{key_note} These are pages of a course "
                       f"PDF (batch {bi + 1}/{len(batches)}).")
        value = None
        for attempt in range(2):
            strict = "" if attempt == 0 else (
                " IMPORTANT: your previous reply was not valid JSON. Reply with "
                "ONLY the JSON array - starting with [ and ending with ].")
            try:
                value = await _llm_json_vision(owner, system, instruction + strict, urls)
                break
            except HTTPException as e:
                if e.status_code == 503:
                    raise
                logger.warning("study vision extract: batch %d attempt %d failed: %s",
                               bi, attempt + 1, e.detail)
                if attempt == 1:
                    errors += 1
        if value is not None:
            if isinstance(value, list):
                raw_count += len(value)
            elif isinstance(value, dict):
                raw_count += len(value.get("questions") or [value])
            fresh = normalize_questions(value)
            _attach_source_pages(
                fresh,
                page_by_number,
                fallback_page=nums[0] if len(nums) == 1 else None,
            )
            collected.extend(fresh)

    # Coverage pass: re-request exactly the questions the manifest says were
    # missed, attaching each one's start page (plus the next, for spillover)
    # and the answer-key pages.
    coverage = _coverage_report(manifest, collected)
    if coverage and coverage["missing"]:
        by_page: Dict[int, List[str]] = {}
        for m in missing_question_numbers(
                manifest, [q.get("number") for q in collected if q.get("number")]):
            by_page.setdefault(m["page"], []).append(m["number"])
        for page_num, nums in sorted(by_page.items()):
            if not (1 <= page_num <= n_pages):
                continue
            idxs = [page_num - 1] + ([page_num] if page_num < n_pages else [])
            urls = [page_urls[i] for i in idxs] + key_urls
            label = ", ".join(nums)
            logger.info("study coverage: re-requesting question(s) %s on page %d",
                        label, page_num)
            instruction = (f"A previous pass missed some questions. Extract ONLY "
                           f"question(s) {label} from these pages, faithfully and "
                           f"completely.{key_note} {type_note}")
            try:
                value = await _llm_json_vision(owner, system, instruction, urls)
                fresh = normalize_questions(value)
                _attach_source_pages(fresh, page_by_number, fallback_page=page_num)
                raw_count += len(fresh)
                collected.extend(fresh)
            except HTTPException as e:
                if e.status_code == 503:
                    raise
                logger.warning("study coverage: targeted page %d failed: %s",
                               page_num, e.detail)
        coverage = _coverage_report(manifest, collected)

    return collected, raw_count, len(batches), errors, coverage, page_info


# Below this the material's notes are a stub or an error line, not a summary,
# and the raw text is the better card source.
CARD_NOTES_MIN_CHARS = 400


def card_source_text(user, material_id: str) -> Dict:
    """Pick the best source text for card generation.

    Prefers the material's AI study notes when they are substantial: they are
    denser and already structured, so cards come out cleaner than from raw
    prose or OCR. Falls back to the material text.
    Returns {text, source: "notes"|"material", material_id, name}."""
    db = SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        notes = (m.summary or "").strip()
        if len(notes) >= CARD_NOTES_MIN_CHARS:
            return {"text": notes, "source": "notes",
                    "material_id": m.id, "name": m.name}
        return {"text": (m.content or "").strip(), "source": "material",
                "material_id": m.id, "name": m.name}
    finally:
        db.close()


CARD_AUTHOR_SYSTEM = """You write flashcards for spaced repetition, following the minimum-information principle.

Rules:
- Each card tests exactly ONE atomic fact, distinction, or step. Split compound ideas into several cards.
- Default to atomic cloze deletions: "front" is the full sentence with the one tested term replaced by _____, "back" is just the missing term. Cloze keeps the retrieval cue in context and forces one fact per card.
- Use a direct question on the "front" instead only where a cloze would be clumsy — why/how/compare/apply cards, or when the sentence would give the answer away. Never a bare topic heading.
- "back" is the shortest complete answer. No filler, no restating the question.
- Prefer why/how/compare/apply cards over pure definitions when the material allows it; understanding beats recognition.
- For formulas: one card for the formula, separate cards for what each symbol means and when to use it.
- Write in the same language as the source material.

Write any mathematics as LaTeX ($...$ inline, $$...$$ display); inside the JSON strings double every backslash (\\\\frac, not \\frac).
Output ONLY a JSON array: [{"front": "...", "back": "..."}, ...]. No markdown fences or commentary around the JSON."""

QUIZ_AUTHOR_SYSTEM = """You write free-recall practice questions for active retrieval, targeting 70-85% expected success (effortful but doable).

Rules:
- Questions demand production, not recognition: explain, derive, compare, apply to a scenario — no multiple choice.
- Mix difficulty; at least one question should apply the material to a new example.
- "reference" is the model answer used for grading: complete but concise.
- Same language as the source material.

Write any mathematics as LaTeX ($...$ inline, $$...$$ display); inside the JSON strings double every backslash (\\\\frac, not \\frac).
Output ONLY a JSON array: [{"question": "...", "reference": "..."}, ...]. No markdown fences or commentary around the JSON."""

GRADER_SYSTEM = """You grade a learner's free-recall answer against a reference answer. Be exacting but fair: grade meaning, not wording. Do not give credit for vague gestures at the topic.

Output ONLY a JSON object:
{"score": 0-100, "verdict": "correct"|"partial"|"incorrect", "feedback": "1-3 sentences: name exactly what was missing or wrong, then the key point to remember", "followup": "one short probing question that targets the weakest part of the answer"}
Use the learner's language for feedback and followup. Write any mathematics as LaTeX ($...$); inside the JSON strings double every backslash (\\\\frac, not \\frac). No markdown fences or commentary around the JSON."""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

# Phase 4.3: shared background-task state for async FSRS optimization.
# Module-level so the status endpoint and tests can access it.
_optimize_locks: Dict[str, threading.Lock] = {}
_optimize_status: Dict[str, Dict] = {}




# ---------------------------------------------------------------------------
# Shared dependencies (extracted from the old setup_study_routes closure)
# ---------------------------------------------------------------------------

_ai_limiter = RateLimiter(max_requests=20, window_seconds=60)


def _owner(request: Request) -> Optional[str]:
    return get_current_user(request)


def _cached_w(db, user: Optional[str]) -> Optional[List[float]]:
    """Return fitted per-user w, if any."""
    if user is None:
        return None
    row = db.query(StudyUserParams).filter(
        StudyUserParams.owner == user,
    ).first()
    if row and row.w_json:
        try:
            return json.loads(row.w_json)
        except Exception:
            pass
    return None


def _new_introduced_today(db, user, deck_id: str) -> int:
    day_start = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(StudyReview).filter(
        StudyReview.deck_id == deck_id,
        StudyReview.state_before == "new",
        StudyReview.reviewed_at >= day_start,
    )
    if user is not None:
        q = q.filter(StudyReview.owner == user)
    return q.count()


def _deck_counts(db, user, deck: StudyDeck) -> Dict:
    now = _utcnow_naive()
    base = db.query(StudyCard).filter(
        StudyCard.deck_id == deck.id, StudyCard.suspended == False)  # noqa: E712
    if user is not None:
        base = base.filter(StudyCard.owner == user)
    due = base.filter(StudyCard.state != "new", StudyCard.due <= now).count()
    new_total = base.filter(StudyCard.state == "new").count()
    cap = max(0, (deck.new_per_day or 0) - _new_introduced_today(db, user, deck.id))
    qbase = db.query(StudyQuestion).filter(
        StudyQuestion.deck_id == deck.id,
        StudyQuestion.suspended == False)  # noqa: E712
    if user is not None:
        qbase = qbase.filter(StudyQuestion.owner == user)
    return {
        "due_count": due,
        "new_available": min(new_total, cap),
        "new_total": new_total,
        "total": base.count(),
        "q_total": qbase.count(),
        "q_due": qbase.filter(StudyQuestion.state != "new",
                              StudyQuestion.due <= now).count(),
        "q_new": qbase.filter(StudyQuestion.state == "new").count(),
    }


# Export *every* module-level name — including underscore-prefixed helpers —
# so the split sub-modules (which do ``from routes.study._common import *``)
# pick up helpers like ``_owner``, ``_card_to_dict``, ``_read_pref``, etc.
import sys as _sys
_public = [n for n in dir(_sys.modules[__name__]) if not n.startswith("__")]
__all__ = _public


async def _llm_text_vision(owner, system: str, instruction: str, image_urls: List[str], *,
                           temperature: float = 0.1, max_tokens: int = EXTRACTION_MAX_TOKENS,
                           timeout: int = 300) -> str:
    """One-shot vision call returning plain text; iterates model candidates."""
    from src.llm_core import llm_call_async

    candidates = _vision_candidates(owner)
    if not candidates:
        raise HTTPException(503, "No vision-capable model available. Pick a vision "
                                 "model in the Study model selector, or configure "
                                 "one in Settings -> Vision.")
    content = [{"type": "text", "text": instruction}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in image_urls
    ]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": content}]
    last_detail = None
    for url, model, headers in candidates:
        try:
            raw = await llm_call_async(url=url, model=model, messages=messages,
                                       temperature=temperature, max_tokens=max_tokens,
                                       headers=headers, timeout=timeout,
                                       extra_body=THINKING_OFF_BODY)
            text = _strip_think_safe(raw, prose=True).strip()
            if text:
                return text
            last_detail = f"Vision model {model} returned an empty reply."
        except HTTPException as e:
            last_detail = f"Vision model {model} failed: {e.detail}"
        except Exception as e:
            last_detail = f"Vision model {model} failed ({type(e).__name__}): {e}."
        logger.warning("study vision text: %s", last_detail)
    raise HTTPException(502, last_detail or "All vision model candidates failed.")
