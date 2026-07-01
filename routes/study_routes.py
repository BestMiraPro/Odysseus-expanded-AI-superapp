# routes/study_routes.py
"""Study module API — evidence-based studying built into Odysseus.

Endpoints:
  /api/study/overview              dashboard: due counts, streak, today's plan
  /api/study/decks ...             subject (deck) CRUD, cards, materials, questions
  /api/study/queue                 FSRS review queue (due + capped new)
  /api/study/cards/{id}/review     apply a rating (FSRS schedule + review log)
  /api/study/ai/generate-cards     LLM: material -> proposed flashcards
  /api/study/materials/{id}/extract     LLM: extract (practice) / author (theory) questions
  /api/study/materials/{id}/transcribe  vision OCR for scanned / formula PDFs
  /api/study/materials/{id}/file         source PDF/image, inline (in-pane viewer)
  /api/study/materials/{id}/notes       AI study notes; /decks/{id}/overview subject map
  /api/study/practice/queue        spaced practice queue (deck / material / topic filters)
  /api/study/questions/{id}/...    attempt (AI-graded), hint, explain, explain-further, locate
  /api/study/exams ...             exam CRUD + deterministic plan generation
  /api/study/focus ...             focus timer sessions
  /api/study/stats, /history       retrieval chart (cards + attempts), history
  /api/study/calibration           confidence calibration (Brier, sure-but-wrong)
  /api/study/agent/...             in-app Study agent (routes/study_agent_routes.py)

Design notes:
- Scheduling is FSRS-4.5 (src/fsrs.py); plans are deterministic
  (src/study_plan.py). The LLM is only used where judgment is needed
  (authoring cards, grading free recall) — never for scheduling.
- The AI endpoints reuse the user's configured Study model (falling back to
  utility, then default).
- The heavy pipelines are module-level ``run_*`` / ``*_payload`` functions so
  the Study agent (src/study_agent.py) calls the same code as the routes.
"""

import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict
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
from src.study_ai import (
    ADD_CONTEXT_SYSTEM,
    ASK_COACH_SYSTEM,
    ASK_ELABORATE_SYSTEM,
    ASK_TUTOR_SYSTEM,
    AUTHOR_QUESTIONS_SYSTEM,
    DISCOVER_QUESTIONS_SYSTEM,
    EXPLAIN_FURTHER_SYSTEM,
    context_is_redundant,
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
    canonical_qnum,
    chunk_material,
    classify_material,
    canonical_qnum,
    confidence_to_numeric,
    dedupe_questions,
    missing_question_numbers,
    normalize_questions,
    offset_manifest,
    parse_answer_key_pages,
    parse_llm_json,
    parse_question_manifest,
    prereqs_from_groups,
    question_is_conclusion,
    question_key,
    rating_from_outcome,
    should_use_vision,
)
from src.study_vision import text_layer_is_thin
from src.auth_helpers import get_current_user
from src.rate_limiter import RateLimiter
from src import fsrs
from src import fsrs_optimizer
from src import study_service
from src.study_source import build_original_question_link, infer_source_page
from src.study_plan import generate_plan, compute_mastery_scores, migrate_done_blocks
from src.study_stats import (
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


class ExamCreate(BaseModel):
    title: str
    exam_date: str  # ISO date
    exam_format: Optional[str] = None
    hours_per_week: float = 7.0
    rest_days: Optional[List[int]] = None
    topics: List[Dict[str, Any]] = []
    deck_id: Optional[str] = None   # linked subject (plan blocks -> practice)


class ExamUpdate(BaseModel):
    title: Optional[str] = None
    exam_date: Optional[str] = None
    exam_format: Optional[str] = None
    hours_per_week: Optional[float] = None
    rest_days: Optional[List[int]] = None
    topics: Optional[List[Dict[str, Any]]] = None
    archived: Optional[bool] = None
    deck_id: Optional[str] = None   # "" clears the link


class ToggleBlockIn(BaseModel):
    key: str  # "YYYY-MM-DD:block_index"


class FocusStart(BaseModel):
    label: Optional[str] = None
    planned_min: int = 25


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
    # None = auto: vision whenever the material is a PDF on disk and a vision-
    # capable model is configured; True/False force one path.
    vision: Optional[bool] = None


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
    answer: Optional[str] = None         # open
    confidence: Optional[int | str] = None  # 0-100 numeric, or legacy label ("sure"/"unsure"/"guess")
    hints_used: int = 0
    duration_ms: Optional[int] = None
    idempotency_key: Optional[str] = None


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
        "archived": bool(exam.archived),
        "deck_id": exam.deck_id,
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


def _study_text_model(owner: Optional[str]) -> str:
    """Optional override model id for text-only Study calls (same endpoint)."""
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        return (get_user_setting("study_text_model", owner or "",
                                 settings.get("study_text_model", "")) or "").strip()
    except Exception:
        return ""


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
    that still reference it (pass it from a grouped query); the stored column
    is only a fallback because it never decremented on deletes."""
    pages = m.page_count or 0
    chars = m.char_count or 0
    return {
        "id": m.id, "deck_id": m.deck_id, "name": m.name, "kind": m.kind,
        "file_id": m.file_id, "char_count": chars,
        "page_count": pages or None,
        # Thin text layer (scanned / formula-image PDF): notes, consult and
        # text extraction can't see the content until it is transcribed.
        "thin_text": bool(m.file_id and pages and text_layer_is_thin(chars, pages)),
        "question_count": (question_count if question_count is not None
                           else (m.question_count or 0)),
        "has_summary": bool(m.summary),
        "category": _material_category(m),
        "created_at": _iso(m.created_at),
    }


def material_rows_with_counts(db, deck_id: str, user) -> List[Dict]:
    """A deck's materials (newest first) with live question counts."""
    q = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
    if user is not None:
        q = q.filter(StudyMaterial.owner == user)
    mats = q.order_by(StudyMaterial.created_at.desc()).all()
    from sqlalchemy import func as _func
    cq = db.query(StudyQuestion.material_id, _func.count(StudyQuestion.id)) \
        .filter(StudyQuestion.deck_id == deck_id)
    if user is not None:
        cq = cq.filter(StudyQuestion.owner == user)
    counts = {mid: n for mid, n in cq.group_by(StudyQuestion.material_id).all()}
    return [_material_to_dict(m, counts.get(m.id, 0)) for m in mats]


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
        out.append({"file_id": mat["file_id"],
                    # The in-pane viewer serves per material, not per upload.
                    "material_id": str(loc.get("material_id") or ""),
                    "name": name, "page": page, "label": label, "url": url})
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

    Returns (normalized_questions, raw_count, batches, errors, coverage,
    page_info) where page_info = {"pages", "total_pages", "truncated"}.
    """
    from src.study_vision import MAX_PAGES, pdf_page_count, render_pdf_pages, pages_to_data_urls

    try:
        page_urls = pages_to_data_urls(render_pdf_pages(pdf_path))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
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

    manifest, answer_key_pages = await _discover_questions_vision(owner, page_urls) \
        if mode == "extract" else ([], [])
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

# ---------------------------------------------------------------------------
# Row getters + counters (module-level so the Study agent shares them)
# ---------------------------------------------------------------------------

def _get_deck(db, deck_id: str, user) -> StudyDeck:
    deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
    if not deck or (user is not None and deck.owner != user):
        raise HTTPException(404, "Deck not found")
    return deck

def _get_card(db, card_id: str, user) -> StudyCard:
    card = db.query(StudyCard).filter(StudyCard.id == card_id).first()
    if not card or (user is not None and card.owner != user):
        raise HTTPException(404, "Card not found")
    return card

def _get_exam(db, exam_id: str, user) -> StudyExam:
    exam = db.query(StudyExam).filter(StudyExam.id == exam_id).first()
    if not exam or (user is not None and exam.owner != user):
        raise HTTPException(404, "Exam not found")
    return exam

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
        m = _get_material(db, material_id, user)
        notes = (m.summary or "").strip()
        if len(notes) >= CARD_NOTES_MIN_CHARS:
            return {"text": notes, "source": "notes",
                    "material_id": m.id, "name": m.name}
        return {"text": (m.content or "").strip(), "source": "material",
                "material_id": m.id, "name": m.name}
    finally:
        db.close()


def _get_material(db, material_id: str, user) -> StudyMaterial:
    m = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not m or (user is not None and m.owner != user):
        raise HTTPException(404, "Material not found")
    return m

def _get_question(db, question_id: str, user) -> StudyQuestion:
    q = db.query(StudyQuestion).filter(StudyQuestion.id == question_id).first()
    if not q or (user is not None and q.owner != user):
        raise HTTPException(404, "Question not found")
    return q



# ---------------------------------------------------------------------------
# Service functions (shared by the routes and the Study agent)
# ---------------------------------------------------------------------------

def _vet_exam_deck(db, deck_id, user) -> Optional[str]:
    """Validate an exam's linked subject; "" / None clears the link."""
    deck_id = (deck_id or "").strip()
    if not deck_id:
        return None
    return _get_deck(db, deck_id, user).id


def create_material_record(user, deck_id: str, *, name: Optional[str] = None,
                           text: Optional[str] = None, file_id: Optional[str] = None,
                           category: Optional[str] = None) -> Dict:
    """Attach source material to a subject: pasted text or a previously
    uploaded file (text is extracted in full; scanned PDFs are kept with an
    empty text layer so vision extraction / transcription can read them).
    Shared by the route and the Study agent."""
    db = SessionLocal()
    try:
        deck = _get_deck(db, deck_id, user)
        page_count = None
        if file_id:
            kind = "pdf" if file_id.lower().endswith(".pdf") else "file"
            name = (name or file_id).strip()
            try:
                text = _extract_file_text(file_id, user)
            except HTTPException as e:
                # Scanned/image-only PDFs have no text layer - keep the
                # material anyway; vision extraction reads the pages.
                if kind == "pdf" and e.status_code == 422:
                    text = ""
                else:
                    raise
            if kind == "pdf":
                try:
                    from src.study_vision import pdf_page_count
                    page_count = pdf_page_count(_resolve_uploaded_file(file_id)) or None
                except Exception:
                    page_count = None
        else:
            text = (text or "").strip()
            kind = "text"
            name = (name or "").strip() or (text[:48] + "…" if len(text) > 48 else text[:48])
        if len(text) < 30 and kind != "pdf":
            raise HTTPException(400, "Provide more material (at least a paragraph).")
        cat = category if category in MATERIAL_CATEGORIES else classify_material(name)
        m = StudyMaterial(
            id=str(uuid.uuid4()), owner=user, deck_id=deck.id,
            name=name[:200], kind=kind, file_id=file_id,
            content=text, char_count=len(text), page_count=page_count,
            category=cat,   # auto-tag on upload; user can change it
        )
        db.add(m)
        db.commit()
        return _material_to_dict(m, 0)
    finally:
        db.close()


async def run_extraction(user, material_id: str, *, mode: str = "extract",
                         types: Optional[List[str]] = None, count: int = 15,
                         vision: Optional[bool] = None) -> Dict:
    """AI question extraction: material -> saved question bank items.

    mode "extract": pull the actual questions out of past papers/problem sets,
    faithfully. mode "author": write new exam-style questions from notes. Long
    materials are chunked; partial results are kept (JSON repair recovers
    complete objects from malformed replies).

    ``vision`` None = auto (vision whenever the PDF is on disk and a vision
    model is configured), True/False = force. Shared by the route and the Study
    agent's ``extract_questions`` tool. Raises HTTPException on user errors.
    """
    mode = mode if mode in ("extract", "author") else "extract"
    types = [t for t in (types or ["mcq", "open"]) if t in ("mcq", "open")] or ["mcq", "open"]
    db = SessionLocal()
    try:
        m = _get_material(db, material_id, user)
        deck_id = m.deck_id
        content = m.content or ""
        file_id = m.file_id
        kind = m.kind
    finally:
        db.close()

    # Locate the original PDF (vision mode and the auto-fallback need it).
    pdf_path = None
    if file_id and (kind == "pdf" or str(file_id).lower().endswith(".pdf")):
        try:
            pdf_path = _resolve_uploaded_file(file_id)
        except HTTPException:
            pdf_path = None

    # Vision by default whenever the original PDF is on disk and a vision-
    # capable model is configured; an explicit ``vision`` flag forces a path.
    use_vision = should_use_vision(
        has_pdf=bool(pdf_path),
        vision_available=bool(pdf_path) and bool(_vision_candidates(user)),
        explicit=vision)
    # Thin text layer (formula images / scans): go vision-first instead of
    # wasting a text pass on cover-page scraps.
    auto_vision = False
    if pdf_path and not use_vision:
        try:
            from src.study_vision import pdf_page_count, text_layer_is_thin
            auto_vision = text_layer_is_thin(len(content), pdf_page_count(pdf_path))
        except Exception:
            auto_vision = False

    system = EXTRACT_QUESTIONS_SYSTEM if mode == "extract" else AUTHOR_QUESTIONS_SYSTEM
    type_note = ("Only produce questions of type: " + ", ".join(types) + ".") \
        if len(types) == 1 else ""

    async def _run_text_pass(text_chunks: List[str]) -> tuple:
        """Per-chunk text extraction. Returns (collected, raw_count, errors)."""
        per_chunk = (max(3, min(40, max(1, int(count or 15))) // len(text_chunks) + 1)
                     if mode == "author" else None)
        t_collected: List[Dict] = []
        t_raw = 0
        t_errors = 0
        for i, chunk in enumerate(text_chunks):
            if mode == "author":
                instruction = (f"Write about {per_chunk} questions from this material "
                               f"(part {i + 1}/{len(text_chunks)}). {type_note}")
            else:
                instruction = (f"Extract every practice question from this material "
                               f"(part {i + 1}/{len(text_chunks)}). {type_note}")
            value = None
            for attempt in range(2):
                strict = "" if attempt == 0 else (
                    "\n\nIMPORTANT: your previous reply was not valid JSON. Reply with "
                    "ONLY the JSON array - it must start with [ and end with ]. "
                    "No prose, no markdown, no explanations.")
                try:
                    value = await _llm_json(user, system,
                                            f"{instruction}{strict}\n\n--- MATERIAL ---\n{chunk}",
                                            temperature=0.2 if mode == "extract" else 0.5,
                                            max_tokens=EXTRACTION_MAX_TOKENS,
                                            timeout=300, thinking_off=True)
                    break
                except HTTPException as e:
                    if e.status_code == 503:
                        raise  # no model configured — fail loudly, not partially
                    logger.warning("study extract: chunk %d attempt %d failed: %s",
                                   i, attempt + 1, e.detail)
                    if attempt == 1:
                        t_errors += 1
            if value is not None:
                if isinstance(value, list):
                    t_raw += len(value)
                elif isinstance(value, dict):
                    t_raw += len(value.get("questions") or [value])
                t_collected.extend(normalize_questions(value))
        return t_collected, t_raw, t_errors

    def _finalize(items: List[Dict]) -> List[Dict]:
        """Type-filter, de-duplicate, and (in extract mode) drop conclusion-
        style 'questions' that leak their own answer."""
        qs = dedupe_questions([q for q in items if q["qtype"] in types])
        if mode == "extract":
            qs = [q for q in qs if not question_is_conclusion(q)]
        return qs

    used_vision = False
    coverage = None
    page_info = None
    if use_vision or auto_vision:
        if not pdf_path:
            raise HTTPException(400, "Vision extraction needs the original PDF "
                                     "file. Re-upload the PDF to this subject.")
        collected, raw_count, n_batches, errors, coverage, page_info = \
            await _extract_questions_vision(user, mode, types, pdf_path)
        used_vision = True
        chunks = [None] * n_batches  # for the response chunk count
    else:
        chunks = chunk_material(content)
        if not chunks and pdf_path:
            # No text layer at all - skip straight to vision.
            collected, raw_count, n_batches, errors, coverage, page_info = \
                await _extract_questions_vision(user, mode, types, pdf_path)
            used_vision = True
            chunks = [None] * n_batches
        elif not chunks:
            raise HTTPException(400, "Material has no text to extract from.")
        else:
            collected, raw_count, errors = await _run_text_pass(chunks)
            # Coverage pass (text): compare against a discovery manifest
            # and re-request anything missed in one targeted call.
            if mode == "extract" and collected:
                manifest = await _discover_questions_text(user, content)
                coverage = _coverage_report(manifest, collected)
                if coverage and coverage["missing"]:
                    nums = ", ".join(coverage["missing"])
                    logger.info("study coverage: re-requesting question(s) %s "
                                "(text)", nums)
                    try:
                        value = await _llm_json(
                            user, system,
                            f"A previous pass missed some questions. Extract "
                            f"ONLY question(s) {nums} from this material, "
                            f"faithfully and completely. {type_note}\n\n"
                            f"--- MATERIAL ---\n{content[:40000]}",
                            temperature=0.2, max_tokens=EXTRACTION_MAX_TOKENS,
                            timeout=300, thinking_off=True)
                        fresh = normalize_questions(value)
                        raw_count += len(fresh)
                        collected.extend(fresh)
                        coverage = _coverage_report(manifest, collected)
                    except HTTPException as e:
                        if e.status_code == 503:
                            raise
                        logger.warning("study coverage: targeted text pass "
                                       "failed: %s", e.detail)

    questions = _finalize(collected)

    # Auto-fallback: text extraction found nothing usable but we have the
    # original PDF — its text layer is probably thin (formula images,
    # scans). Try vision before giving up.
    if not questions and not used_vision and pdf_path:
        logger.info("study extract: text pass empty for material %s — "
                    "falling back to vision extraction", material_id)
        try:
            v_collected, v_raw, v_batches, v_errors, v_coverage, v_page_info = \
                await _extract_questions_vision(user, mode, types, pdf_path)
            if v_collected:
                collected, raw_count, errors = v_collected, v_raw, v_errors
                chunks = [None] * v_batches
                used_vision = True
                coverage = v_coverage
                page_info = v_page_info
                questions = _finalize(collected)
        except HTTPException as e:
            logger.warning("study extract: vision fallback unavailable: %s", e.detail)

    # Mirror fallback: vision found nothing usable but the material HAS a
    # text layer (e.g. the vision model can't read images, or vision was
    # forced on a text-rich PDF). Try text before giving up.
    if not questions and used_vision:
        text_chunks = chunk_material(content)
        if text_chunks:
            logger.info("study extract: vision pass empty for material %s — "
                        "falling back to text extraction", material_id)
            try:
                t_collected, t_raw, t_errors = await _run_text_pass(text_chunks)
                if t_collected:
                    collected, raw_count, errors = t_collected, t_raw, t_errors
                    chunks = text_chunks
                    used_vision = False
                    coverage = None  # manifest came from the failed pass
                    questions = _finalize(collected)
            except HTTPException as e:
                logger.warning("study extract: text fallback failed: %s", e.detail)

    if not questions:
        if errors >= len(chunks):
            detail = ("Every chunk failed: the model's replies were empty or "
                      "not parseable as JSON (the server log has the raw "
                      "replies). Empty replies usually mean the reply was "
                      "truncated mid-reasoning; retry, or switch the Study "
                      "model (the model selector in the top bar).")
        elif raw_count == 0:
            detail = ("The model returned valid JSON but found no questions in "
                      "this material. If it is notes rather than an exam, use "
                      "'Author questions' instead of 'Extract questions'.")
        elif types != ["mcq", "open"]:
            detail = (f"{raw_count} question(s) were found but none matched the "
                      f"requested type filter ({', '.join(types)}).")
        else:
            detail = (f"The model found {raw_count} question(s) but none were "
                      "usable (e.g. MCQs whose correct answer could not be "
                      "identified). Try again or switch the Study model.")
        raise HTTPException(502, detail)

    db = SessionLocal()
    try:
        now = _utcnow_naive()
        # Cross-run dedupe: never re-add a question already in this deck, so
        # re-running extraction (or extracting overlapping materials) tops up
        # the bank instead of duplicating it.
        existing_keys = {
            question_key(text) for (text,) in
            db.query(StudyQuestion.question)
              .filter(StudyQuestion.deck_id == deck_id).all()
        }
        saved = []
        duplicates = 0
        for q in questions:
            key = question_key(q["question"])
            if key in existing_keys:
                duplicates += 1
                continue
            existing_keys.add(key)
            row = StudyQuestion(
                id=str(uuid.uuid4()), owner=user, deck_id=deck_id,
                material_id=material_id, qtype=q["qtype"],
                question=q["question"],
                context=q.get("context"),
                options=json.dumps(q["options"]) if q["options"] else None,
                correct_index=q["correct_index"], reference=q["reference"],
                topic=q["topic"], difficulty=q["difficulty"],
                number=q.get("number"),
                origin="extracted" if mode == "extract" else "authored",
                state="new", due=now,
            )
            db.add(row)
            saved.append(row)
        m = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
        if m:
            m.question_count = (m.question_count or 0) + len(saved)
        db.commit()
        resp = {
            "created": len(saved),
            "duplicates": duplicates,
            "chunks": len(chunks),
            "chunk_errors": errors,
            "vision": used_vision,
            "coverage": coverage,
            "pages": page_info,
            "questions": [_question_to_dict(r) for r in saved],
        }
        created_n = len(saved)
    finally:
        db.close()

    # Auto-link multi-part problems for this material so practice immediately
    # shows earlier parts + answers as context. Best-effort — an extraction
    # must never fail because grouping did.
    if created_n:
        try:
            await _link_deck_parts(user, deck_id, only_material=material_id)
        except Exception as e:
            logger.warning("study: auto link-parts after extraction failed: %s", e)
    return resp


async def run_generate_notes(user, material_id: str) -> Dict:
    """Generate (or regenerate) consultable study notes for one material: a
    Markdown summary from the full text, plus a Key-figures section with figures
    pulled from the source PDF and cited to their page. Returns {summary,
    has_figures}. Shared by the route and the Study agent."""
    db = SessionLocal()
    try:
        m = _get_material(db, material_id, user)
        content = (m.content or "").strip()
        name, file_id, kind = m.name, m.file_id, m.kind
    finally:
        db.close()
    if len(content) < 200:
        raise HTTPException(400, "Not enough text in this material to write "
                                 "notes. If it is a scanned PDF, run vision "
                                 "extraction or re-extract its text first.")
    notes = await _llm_text(
        user, STUDY_NOTES_SYSTEM,
        f"Material name: {name}\n\n--- MATERIAL ---\n{content[:120000]}",
        temperature=0.3, max_tokens=8000, timeout=240)

    figures_md = ""
    pdf_path = None
    if file_id and (kind == "pdf" or str(file_id).lower().endswith(".pdf")):
        try:
            pdf_path = _resolve_uploaded_file(file_id)
        except HTTPException:
            pdf_path = None
    if pdf_path:
        figures_md = await _build_figures_section(user, material_id, file_id, pdf_path)

    full = notes.strip() + figures_md
    db = SessionLocal()
    try:
        m = _get_material(db, material_id, user)
        m.summary = full
        db.commit()
    finally:
        db.close()
    return {"summary": full, "has_figures": bool(figures_md)}


async def run_generate_overview(user, deck_id: str) -> Dict:
    """Generate a short subject overview from the chapter notes (preferred) or
    raw material text, tying the chapters together. Returns {overview}."""
    db = SessionLocal()
    try:
        deck = _get_deck(db, deck_id, user)
        deck_name = deck.name
        q = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
        if user is not None:
            q = q.filter(StudyMaterial.owner == user)
        parts = []
        for m in q.order_by(StudyMaterial.created_at.asc()).all():
            src = (m.summary or m.content or "")[:4000].strip()
            if src:
                parts.append(f"### {m.name}\n{src}")
    finally:
        db.close()
    if not parts:
        raise HTTPException(400, "Add materials (and ideally generate chapter "
                                 "notes) before generating a subject overview.")
    prompt = (f"Subject: {deck_name}\n\n" + "\n\n".join(parts))[:60000]
    overview = await _llm_text(user, SUBJECT_OVERVIEW_SYSTEM, prompt,
                               temperature=0.3, max_tokens=4000, timeout=180)
    db = SessionLocal()
    try:
        deck = _get_deck(db, deck_id, user)
        deck.overview = overview
        db.commit()
    finally:
        db.close()
    return {"overview": overview}


TRANSCRIBE_PAGES_PER_CALL = 3


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


async def run_transcribe_material(user, material_id: str) -> Dict:
    """Vision OCR for a scanned / formula-image PDF: transcribe every page to
    Markdown (LaTeX math), TRANSCRIBE_PAGES_PER_CALL pages per call, and store
    it as the material's text with ``[Page N text]:`` markers — the same shape
    the PDF text extractor produces — so notes, consult, explain-further and
    text extraction work on it. Returns {char_count, pages, pages_failed}."""
    from src.study_vision import pages_to_data_urls, render_pdf_pages

    db = SessionLocal()
    try:
        m = _get_material(db, material_id, user)
        if not m.file_id:
            raise HTTPException(400, "Only file-backed PDF materials can be transcribed.")
        file_id = m.file_id
    finally:
        db.close()
    pdf_path = _resolve_uploaded_file(file_id)
    try:
        urls = pages_to_data_urls(render_pdf_pages(pdf_path))
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    parts: List[str] = []
    failed = 0
    for start in range(0, len(urls), TRANSCRIBE_PAGES_PER_CALL):
        batch = urls[start:start + TRANSCRIBE_PAGES_PER_CALL]
        instruction = (f"Transcribe these {len(batch)} pages (pages {start + 1}-"
                       f"{start + len(batch)} of the document). Start each page with "
                       f"a line '[Page N text]:' using the document page number.")
        try:
            text = await _llm_text_vision(user, TRANSCRIBE_SYSTEM, instruction, batch)
        except HTTPException as e:
            if e.status_code == 503:
                raise
            failed += len(batch)
            logger.warning("study transcribe: pages %d+ failed: %s", start + 1, e.detail)
            continue
        # Guarantee the page markers even if the model dropped them.
        if "[Page" not in text:
            text = f"[Page {start + 1} text]:\n{text}"
        parts.append(text.strip())
    full = "\n\n".join(parts).strip()
    if len(full) < 30:
        raise HTTPException(502, "The vision model returned no usable transcription.")
    db = SessionLocal()
    try:
        m = _get_material(db, material_id, user)
        m.content = full
        m.char_count = len(full)
        m.page_count = m.page_count or len(urls)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "char_count": len(full), "pages": len(urls), "pages_failed": failed}


def _split_topics(topics) -> List[str]:
    """Normalize a topics filter (list or comma-separated string) to lowercase
    non-empty labels."""
    if not topics:
        return []
    items = topics if isinstance(topics, (list, tuple)) else str(topics).split(",")
    return [t.strip().lower() for t in items if t and t.strip()]


def _round_robin(rows: List, key) -> List:
    """Reorder rows so consecutive items come from different groups, preserving
    each group's internal order. Groups are visited in first-appearance order,
    so the highest-priority group still leads."""
    groups: Dict[str, List] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out: List = []
    while groups:
        for k in list(groups.keys()):
            out.append(groups[k].pop(0))
            if not groups[k]:
                del groups[k]
    return out


def practice_queue_payload(user, *, deck_id: Optional[str] = None,
                           material_id: Optional[str] = None, topics=None,
                           limit: int = 20, mock: bool = False) -> Dict:
    """Build the practice queue: due questions first (spaced retrieval), then
    new ones interleaved across topics (round-robin) instead of blocked.

    Scope: all decks, one deck, one material, and/or a topic filter (labels are
    matched case-insensitively as substrings of the question's topic, OR-ed).
    A topic filter that matches nothing is dropped (``topic_fallback``) rather
    than returning an empty session — exam-plan topics rarely spell the
    extractor's labels exactly.

    ``mock=True`` builds a timed full-format paper instead: a fixed number of
    questions drawn from the whole scope regardless of FSRS state, interleaved
    across topics. A mock measures where you stand today, so it must be able to
    ask questions that are not due yet."""
    limit = max(1, min(100, limit))
    wanted = _split_topics(topics)
    db = SessionLocal()
    try:
        now = _utcnow_naive()
        base = db.query(StudyQuestion).filter(
            StudyQuestion.suspended == False)  # noqa: E712
        if material_id:
            m = _get_material(db, material_id, user)
            base = base.filter(StudyQuestion.material_id == m.id)
            deck_id = deck_id or m.deck_id
        if deck_id:
            _get_deck(db, deck_id, user)
            base = base.filter(StudyQuestion.deck_id == deck_id)
        if user is not None:
            base = base.filter(StudyQuestion.owner == user)

        # A session scoped to one subject (or material) has nothing to
        # interleave across; "practice everything" does.
        scoped_to_one = bool(material_id or deck_id)

        def _pull(q):
            if mock:
                # A mock ignores the schedule: draw the whole scope, oldest
                # first, and let the topic round-robin below shape the paper.
                return [], q.order_by(StudyQuestion.created_at.asc()).limit(
                    max(limit * 4, 100)).all()
            due_base = q.filter(StudyQuestion.state != "new",
                                StudyQuestion.due <= now)
            if scoped_to_one:
                due = due_base.order_by(StudyQuestion.due.asc()).limit(limit).all()
            else:
                # Take each subject's most-overdue questions and round-robin
                # them, so a large backlog in one subject cannot crowd every
                # other subject out of the session.
                deck_ids = [r[0] for r in due_base.with_entities(
                    StudyQuestion.deck_id).distinct().all()]
                pooled = []
                for did in deck_ids:
                    pooled.extend(
                        due_base.filter(StudyQuestion.deck_id == did)
                        .order_by(StudyQuestion.due.asc()).limit(limit).all())
                pooled.sort(key=lambda r: (r.due or now))
                due = _round_robin(pooled, lambda r: r.deck_id or "")[:limit]
            new_rows = q.filter(StudyQuestion.state == "new") \
                .order_by(StudyQuestion.created_at.asc()).limit(limit * 3).all()
            return due, new_rows

        topic_fallback = False
        if wanted:
            from sqlalchemy import or_
            tq = base.filter(or_(*[StudyQuestion.topic.ilike(f"%{t}%") for t in wanted]))
            due, new_rows = _pull(tq)
            if not due and not new_rows:
                topic_fallback = True
                due, new_rows = _pull(base)
        else:
            due, new_rows = _pull(base)

        # Interleave new questions across subject *and* topic, so an unscoped
        # session mixes subjects here too (within one subject the deck part of
        # the key is constant and this stays the old topic round-robin).
        interleaved = _round_robin(
            new_rows, lambda r: f"{r.deck_id or ''}|{r.topic or 'general'}")[:limit]
        queue = due + interleaved
        return {"queue": [_question_to_dict(r, with_answer=False)
                          for r in queue[:limit]],
                "due": len(due), "total": len(queue),
                "mock": bool(mock),
                "topic_fallback": topic_fallback}
    finally:
        db.close()


def overview_payload(user) -> Dict:
    """Dashboard payload: per-deck due/new counts, today's retrievals, streak,
    focus minutes and upcoming exams with today's plan blocks. Shared by the
    /overview route and the Study agent's ``study_stats`` tool."""
    db = SessionLocal()
    try:
        now = _utcnow_naive()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        deck_q = db.query(StudyDeck).filter(StudyDeck.archived == False)  # noqa: E712
        if user is not None:
            deck_q = deck_q.filter(StudyDeck.owner == user)
        decks = deck_q.all()
        deck_rows = []
        due_total = new_total = q_due_total = q_new_total = 0
        for d in decks:
            counts = _deck_counts(db, user, d)
            due_total += counts["due_count"]
            new_total += counts["new_available"]
            q_due_total += counts["q_due"]
            q_new_total += counts["q_new"]
            deck_rows.append({"id": d.id, "name": d.name, "color": d.color,
                              **counts})

        rev_q = db.query(StudyReview)
        if user is not None:
            rev_q = rev_q.filter(StudyReview.owner == user)
        today_reviews = rev_q.filter(StudyReview.reviewed_at >= day_start).all()
        today_again = sum(1 for r in today_reviews if r.rating == 1)
        success = (1 - today_again / len(today_reviews)) if today_reviews else None

        # Streak: consecutive days with >= 1 review, counting back from
        # today (or yesterday if today has none yet).
        recent = rev_q.filter(
            StudyReview.reviewed_at >= now - timedelta(days=120)).all()
        days_with = {r.reviewed_at.date() for r in recent if r.reviewed_at}
        recent_att_q = db.query(StudyAttempt).filter(
            StudyAttempt.attempted_at >= now - timedelta(days=120))
        if user is not None:
            recent_att_q = recent_att_q.filter(StudyAttempt.owner == user)
        days_with |= {t.attempted_at.date() for t in recent_att_q.all()
                      if t.attempted_at}
        streak = 0
        probe = now.date()
        if probe not in days_with:
            probe -= timedelta(days=1)
        while probe in days_with:
            streak += 1
            probe -= timedelta(days=1)

        focus_q = db.query(StudyFocusSession).filter(
            StudyFocusSession.started_at >= day_start)
        if user is not None:
            focus_q = focus_q.filter(StudyFocusSession.owner == user)
        focus_min = sum(s.actual_min or 0 for s in focus_q.all())

        exam_q = db.query(StudyExam).filter(StudyExam.archived == False)  # noqa: E712
        if user is not None:
            exam_q = exam_q.filter(StudyExam.owner == user)
        exams = []
        today_iso = now.date().isoformat()
        for e in exam_q.order_by(StudyExam.exam_date.asc()).all():
            try:
                days_left = (date.fromisoformat(e.exam_date) - now.date()).days
            except ValueError:
                days_left = None
            if days_left is not None and days_left < 0:
                continue
            plan = json.loads(e.plan) if e.plan else None
            done = set(json.loads(e.done_blocks) if e.done_blocks else [])
            today_blocks = []
            if plan:
                for d in plan.get("days", []):
                    if d.get("date") == today_iso:
                        for i, b in enumerate(d.get("blocks", [])):
                            today_blocks.append({
                                **b, "key": f"{today_iso}:{i}",
                                "done": f"{today_iso}:{i}" in done,
                            })
            exams.append({"id": e.id, "title": e.title, "exam_date": e.exam_date,
                          "deck_id": e.deck_id,
                          "days_left": days_left, "has_plan": bool(plan),
                          "today_blocks": today_blocks})

        att_q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= day_start)
        if user is not None:
            att_q = att_q.filter(StudyAttempt.owner == user)
        today_attempts = att_q.all()
        att_ok = sum(1 for t in today_attempts if _attempt_ok(t))

        return {
            "decks": deck_rows,
            "due_total": due_total,
            "new_total": new_total,
            "q_due_total": q_due_total,
            "q_new_total": q_new_total,
            "today": {
                "attempts": len(today_attempts),
                "attempts_ok": att_ok,
                "reviews": len(today_reviews),
                "success_rate": round(success, 3) if success is not None else None,
                "focus_min": focus_min,
                "streak_days": streak,
            },
            "exams": exams,
        }
    finally:
        db.close()


def history_entries(user, limit: int = 100) -> Dict:
    """Answered-question + card-review log, newest first. Reads the existing
    append-only StudyAttempt / StudyReview tables joined to their text."""
    limit = max(1, min(500, limit))
    db = SessionLocal()
    try:
        aq = db.query(StudyAttempt)
        if user is not None:
            aq = aq.filter(StudyAttempt.owner == user)
        attempts = aq.order_by(StudyAttempt.attempted_at.desc()).limit(limit).all()
        q_ids = {a.question_id for a in attempts}
        qmap = {qq.id: qq for qq in db.query(StudyQuestion).filter(
            StudyQuestion.id.in_(q_ids)).all()} if q_ids else {}

        entries = []
        for a in attempts:
            q = qmap.get(a.question_id)
            grading = None
            if a.grading:
                try:
                    grading = json.loads(a.grading)
                except Exception:
                    grading = None
            entries.append({
                "kind": "question",
                "when": _iso(a.attempted_at),
                "deck_id": a.deck_id,
                "qtype": a.qtype,
                "title": q.question if q else "(question deleted)",
                "answer": a.answer,
                "correct": a.correct,
                "score": a.score,
                "rating": a.rating,
                "confidence": a.confidence,
                "hints_used": a.hints_used or 0,
                "feedback": (grading or {}).get("feedback"),
                "followup": (grading or {}).get("followup"),
                "reference": q.reference if q else None,
            })

        rq = db.query(StudyReview)
        if user is not None:
            rq = rq.filter(StudyReview.owner == user)
        reviews = rq.order_by(StudyReview.reviewed_at.desc()).limit(limit).all()
        c_ids = {r.card_id for r in reviews}
        cmap = {cc.id: cc for cc in db.query(StudyCard).filter(
            StudyCard.id.in_(c_ids)).all()} if c_ids else {}
        for r in reviews:
            c = cmap.get(r.card_id)
            entries.append({
                "kind": "card",
                "when": _iso(r.reviewed_at),
                "deck_id": r.deck_id,
                "title": c.front if c else "(card deleted)",
                "back": c.back if c else None,
                "rating": r.rating,
            })

        entries.sort(key=lambda e: e["when"] or "", reverse=True)
        return {"entries": entries[:limit]}
    finally:
        db.close()



def _attempt_ok(a) -> bool:
    """Did a practice attempt count as a successful retrieval? MCQs are graded
    right/wrong; open answers pass at the same 60% mark the dashboard uses."""
    return (a.correct is True) or ((a.score or 0) >= 60)


def stats_payload(user, days: int = 42) -> Dict:
    """Daily retrieval chart + totals for the last ``days`` days.

    Counts both kinds of retrieval — card reviews *and* practice-question
    attempts. Practice is where most retrieval happens now, so a chart fed by
    card reviews alone under-reports both the work done and the success rate.
    Shared by the /stats route and the Study agent."""
    days = max(7, min(180, days))
    db = SessionLocal()
    try:
        now = _utcnow_naive()
        since = now - timedelta(days=days)
        rev_q = db.query(StudyReview).filter(StudyReview.reviewed_at >= since)
        att_q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= since)
        foc_q = db.query(StudyFocusSession).filter(
            StudyFocusSession.started_at >= since)
        if user is not None:
            rev_q = rev_q.filter(StudyReview.owner == user)
            att_q = att_q.filter(StudyAttempt.owner == user)
            foc_q = foc_q.filter(StudyFocusSession.owner == user)

        by_day: Dict[str, Dict] = {}
        for i in range(days + 1):
            d = (since + timedelta(days=i)).date().isoformat()
            by_day[d] = {"date": d, "reviews": 0, "again": 0,
                         "attempts": 0, "attempts_ok": 0, "focus_min": 0}
        for r in rev_q.all():
            k = r.reviewed_at.date().isoformat() if r.reviewed_at else None
            if k in by_day:
                by_day[k]["reviews"] += 1
                if r.rating == 1:
                    by_day[k]["again"] += 1
        for t in att_q.all():
            k = t.attempted_at.date().isoformat() if t.attempted_at else None
            if k in by_day:
                by_day[k]["attempts"] += 1
                if _attempt_ok(t):
                    by_day[k]["attempts_ok"] += 1
        for s in foc_q.all():
            k = s.started_at.date().isoformat() if s.started_at else None
            if k in by_day:
                by_day[k]["focus_min"] += s.actual_min or 0

        total_reviews = sum(v["reviews"] for v in by_day.values())
        total_again = sum(v["again"] for v in by_day.values())
        total_attempts = sum(v["attempts"] for v in by_day.values())
        total_att_ok = sum(v["attempts_ok"] for v in by_day.values())
        # One blended retrieval rate: recalled cards + correct attempts over
        # every graded retrieval in the window.
        retrievals = total_reviews + total_attempts
        recalled = (total_reviews - total_again) + total_att_ok

        card_q = db.query(StudyCard)
        if user is not None:
            card_q = card_q.filter(StudyCard.owner == user)
        return {
            "daily": sorted(by_day.values(), key=lambda v: v["date"]),
            "totals": {
                "reviews": total_reviews,
                "attempts": total_attempts,
                "retrievals": retrievals,
                "success_rate": round(recalled / retrievals, 3) if retrievals else None,
                "cards": card_q.count(),
                "focus_min": sum(v["focus_min"] for v in by_day.values()),
            },
        }
    finally:
        db.close()


# What each confidence tag claims as a probability of being right. Used to
# score the tags: "sure" is a near-certain call, "guess" sits above an MCQ's
# chance floor because a guess with elimination still beats random.
CONFIDENCE_P = {"sure": 0.9, "unsure": 0.6, "guess": 0.3}


def calibration_payload(user, days: int = 90) -> Dict:
    """Confidence calibration over the last ``days`` days.

    Every practice answer carries a sure/unsure/guess tag, so each attempt is a
    probability forecast that can be scored. Reports the Brier score (mean
    squared error of those forecasts — lower is better, 0.25 is what you would
    get by saying 50% to everything), stated-vs-actual accuracy per bucket, and
    the "sure but wrong" rate per subject: the misses worth re-testing first.
    Shared by the /calibration route and the Study agent."""
    days = max(7, min(365, days))
    db = SessionLocal()
    try:
        now = _utcnow_naive()
        since = now - timedelta(days=days)
        att_q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= since)
        if user is not None:
            att_q = att_q.filter(StudyAttempt.owner == user)
        attempts = att_q.all()

        # Only tagged attempts with an actual outcome can be scored.
        graded = [a for a in attempts
                  if (a.confidence or "").lower() in CONFIDENCE_P
                  and (a.correct is not None or a.score is not None)]

        def _brier(rows) -> Optional[float]:
            if not rows:
                return None
            total = sum((CONFIDENCE_P[(a.confidence or "").lower()]
                         - (1.0 if _attempt_ok(a) else 0.0)) ** 2 for a in rows)
            return round(total / len(rows), 3)

        def _rate(hit: int, n: int) -> Optional[float]:
            return round(hit / n, 3) if n else None

        buckets = []
        for label, expected in CONFIDENCE_P.items():
            rows = [a for a in graded if (a.confidence or "").lower() == label]
            correct = sum(1 for a in rows if _attempt_ok(a))
            accuracy = _rate(correct, len(rows))
            buckets.append({
                "confidence": label,
                "expected": expected,
                "attempts": len(rows),
                "correct": correct,
                "accuracy": accuracy,
                # Negative = overconfident (claimed more than delivered).
                "gap": round(accuracy - expected, 3) if accuracy is not None else None,
            })

        sure_rows = [a for a in graded if (a.confidence or "").lower() == "sure"]
        sure_wrong_rows = [a for a in sure_rows if not _attempt_ok(a)]

        by_deck_rows: Dict[str, List] = {}
        for a in graded:
            by_deck_rows.setdefault(a.deck_id or "", []).append(a)
        names = {}
        if by_deck_rows:
            dq = db.query(StudyDeck).filter(StudyDeck.id.in_(list(by_deck_rows)))
            names = {d.id: d.name for d in dq.all()}
        by_deck = []
        for did, rows in by_deck_rows.items():
            d_sure = [a for a in rows if (a.confidence or "").lower() == "sure"]
            d_sure_wrong = [a for a in d_sure if not _attempt_ok(a)]
            by_deck.append({
                "deck_id": did or None,
                "name": names.get(did, "(subject deleted)"),
                "attempts": len(rows),
                "correct": sum(1 for a in rows if _attempt_ok(a)),
                "sure": len(d_sure),
                "sure_wrong": len(d_sure_wrong),
                "sure_wrong_rate": _rate(len(d_sure_wrong), len(d_sure)),
                "brier": _brier(rows),
            })
        # Worst calibration first — that is where the re-tests belong.
        by_deck.sort(key=lambda r: (-(r["sure_wrong_rate"] or 0), -r["attempts"]))

        return {
            "days": days,
            "overall": {
                "attempts": len(attempts),
                "graded": len(graded),
                "correct": sum(1 for a in graded if _attempt_ok(a)),
                "brier": _brier(graded),
                "sure": len(sure_rows),
                "sure_wrong": len(sure_wrong_rows),
                "sure_wrong_rate": _rate(len(sure_wrong_rows), len(sure_rows)),
            },
            "buckets": buckets,
            "by_deck": by_deck,
        }
    finally:
        db.close()


def run_dedup(user, deck_id: Optional[str] = None) -> Dict:
    """Remove duplicate questions (same notation-insensitive key); keeps the
    best copy of each cluster. Scoped to one subject when ``deck_id`` is given —
    the Tidy-bank UI runs per subject — else across all of them.
    {deleted, by_deck}."""
    db = SessionLocal()
    try:
        if deck_id:
            _get_deck(db, deck_id, user)
            deck_ids = [deck_id]
        else:
            dq = db.query(StudyDeck)
            if user is not None:
                dq = dq.filter(StudyDeck.owner == user)
            deck_ids = [d.id for d in dq.all()]
        out = {}
        total = 0
        for did in deck_ids:
            n = _dedup_deck_questions(db, did, user)
            if n:
                out[did] = n
                total += n
    finally:
        db.close()
    return {"deleted": total, "by_deck": out}


async def run_backfill_context(user, deck_id: Optional[str] = None) -> Dict:
    """Recover the shared problem setup for multi-part questions split at
    extraction (idempotent; questions with context are skipped). Scoped to one
    subject when ``deck_id`` is given. {filled, scanned}."""
    db = SessionLocal()
    try:
        qq = db.query(StudyQuestion)
        if user is not None:
            qq = qq.filter(StudyQuestion.owner == user)
        if deck_id:
            _get_deck(db, deck_id, user)
            qq = qq.filter(StudyQuestion.deck_id == deck_id)
        rows = qq.order_by(StudyQuestion.created_at.asc()).all()
        by_mat = defaultdict(list)
        for r in rows:
            if (r.context or "").strip():       # idempotent
                continue
            if not r.material_id:               # no source to recover from
                continue
            by_mat[r.material_id].append(
                {"id": r.id, "number": r.number, "question": r.question[:600]})
        mats: Dict[str, str] = {}
        if by_mat:
            for m in db.query(StudyMaterial).filter(
                    StudyMaterial.id.in_(list(by_mat))).all():
                mats[m.id] = m.content or ""
        valid = {r.id for r in rows}
    finally:
        db.close()

    results: Dict[str, str] = {}
    scanned = 0
    for mat_id, items in by_mat.items():
        mat_text = mats.get(mat_id, "")
        if not mat_text.strip():
            continue
        scanned += len(items)
        updates = await _backfill_context_items(user, mat_text, items)
        for qid, ctx in updates.items():
            if qid in valid:
                results[qid] = ctx

    filled = 0
    db = SessionLocal()
    try:
        for r in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(list(results))).all()
                  if results else []):
            if not (r.context or "").strip():
                r.context = results[r.id]
                filled += 1
        db.commit()
    finally:
        db.close()
    return {"filled": filled, "scanned": scanned}


async def run_reformat(user, deck_id: Optional[str] = None) -> Dict:
    """Reformat existing questions and cards to LaTeX (math) + Markdown,
    preserving content. Idempotent — items already using $ are skipped. Touches
    only text fields; answers/correct_index are untouched. Scoped to one
    subject when ``deck_id`` is given. {questions, cards, *_scanned}."""
    db = SessionLocal()
    try:
        qq = db.query(StudyQuestion)
        cc = db.query(StudyCard)
        if user is not None:
            qq = qq.filter(StudyQuestion.owner == user)
            cc = cc.filter(StudyCard.owner == user)
        if deck_id:
            _get_deck(db, deck_id, user)
            qq = qq.filter(StudyQuestion.deck_id == deck_id)
            cc = cc.filter(StudyCard.deck_id == deck_id)
        q_items, q_opts = [], {}
        for q in qq.all():
            opts = json.loads(q.options) if q.options else None
            if not _needs_reformat(q.question, q.reference or "",
                                   " ".join(opts or [])):
                continue
            item = {"id": q.id, "question": q.question}
            if opts:
                item["options"] = opts
            if q.reference:
                item["reference"] = q.reference
            q_items.append(item)
            q_opts[q.id] = opts
        c_items = []
        for c in cc.all():
            if not _needs_reformat(c.front, c.back):
                continue
            c_items.append({"id": c.id, "front": c.front, "back": c.back})
    finally:
        db.close()

    q_updates = await _reformat_items(user, q_items)
    c_updates = await _reformat_items(user, c_items)

    q_n = c_n = 0
    db = SessionLocal()
    try:
        for q in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(list(q_updates)))
                  .all() if q_updates else []):
            u = q_updates.get(q.id) or {}
            changed = False
            if isinstance(u.get("question"), str) and u["question"].strip():
                q.question = u["question"]; changed = True
            if "reference" in u and isinstance(u["reference"], str):
                q.reference = u["reference"]; changed = True
            # Only replace options if the count matches (keeps correct_index valid).
            orig = q_opts.get(q.id)
            if isinstance(u.get("options"), list) and orig and len(u["options"]) == len(orig):
                q.options = json.dumps([str(o) for o in u["options"]]); changed = True
            if changed:
                q.explanation = None  # cached MCQ explanation may be stale
                q_n += 1
        for c in (db.query(StudyCard).filter(StudyCard.id.in_(list(c_updates)))
                  .all() if c_updates else []):
            u = c_updates.get(c.id) or {}
            if isinstance(u.get("front"), str) and u["front"].strip():
                c.front = u["front"]
            if isinstance(u.get("back"), str) and u["back"].strip():
                c.back = u["back"]
            c_n += 1
        db.commit()
    finally:
        db.close()
    return {"questions": q_n, "cards": c_n,
            "questions_scanned": len(q_items), "cards_scanned": len(c_items)}


async def run_audit_questions(user, deck_id: Optional[str] = None) -> Dict:
    """Suspend 'questions' that state their own answer (solution steps leaked
    at extraction). Reversible in the bank. Scoped to one subject when
    ``deck_id`` is given. {flagged, suspended, scanned}."""
    db = SessionLocal()
    try:
        qq = db.query(StudyQuestion).filter(StudyQuestion.qtype == "open")
        if user is not None:
            qq = qq.filter(StudyQuestion.owner == user)
        if deck_id:
            _get_deck(db, deck_id, user)
            qq = qq.filter(StudyQuestion.deck_id == deck_id)
        rows = [r for r in qq.all() if not r.suspended]
        items = [{"id": r.id, "question": (r.question or "")[:600]} for r in rows]
        # Deterministic layer: obvious conclusion openers, caught for free.
        deterministic = {
            r.id for r in rows
            if question_is_conclusion({"qtype": "open", "question": r.question or ""})
        }
    finally:
        db.close()

    flagged = await _audit_solution_statements(user, items)
    flagged |= deterministic

    suspended = 0
    db = SessionLocal()
    try:
        for r in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(list(flagged))).all()
                  if flagged else []):
            if not r.suspended:
                r.suspended = True
                suspended += 1
        db.commit()
    finally:
        db.close()
    return {"flagged": len(flagged), "suspended": suspended, "scanned": len(items)}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def setup_study_routes():
    router = APIRouter(prefix="/api/study", tags=["study"])

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

    # ------------------------------------------------------------------ decks

    @router.get("/decks")
    def list_decks(request: Request, archived: bool = False):
        user = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(StudyDeck).filter(StudyDeck.archived == archived)
            if user is not None:
                q = q.filter(StudyDeck.owner == user)
            decks = q.order_by(StudyDeck.created_at.asc()).all()
            out = []
            for d in decks:
                row = {
                    "id": d.id, "name": d.name, "description": d.description,
                    "color": d.color, "archived": bool(d.archived),
                    "new_per_day": d.new_per_day, "retention": _flt(d.retention, 0.9),
                }
                row.update(_deck_counts(db, user, d))
                out.append(row)
            return {"decks": out}
        finally:
            db.close()

    @router.post("/decks")
    def create_deck(request: Request, body: DeckCreate):
        user = _owner(request)
        if not body.name.strip():
            raise HTTPException(400, "Deck name is required")
        db = SessionLocal()
        try:
            deck = StudyDeck(
                id=str(uuid.uuid4()), owner=user, name=body.name.strip(),
                description=body.description, color=body.color,
                new_per_day=max(0, body.new_per_day),
                retention=str(min(0.99, max(0.7, body.retention))),
            )
            db.add(deck)
            db.commit()
            return {"id": deck.id, "name": deck.name}
        finally:
            db.close()

    @router.put("/decks/{deck_id}")
    def update_deck(request: Request, deck_id: str, body: DeckUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            if body.name is not None:
                deck.name = body.name.strip() or deck.name
            if body.description is not None:
                deck.description = body.description
            if body.color is not None:
                deck.color = body.color
            if body.new_per_day is not None:
                deck.new_per_day = max(0, body.new_per_day)
            if body.retention is not None:
                deck.retention = str(min(0.99, max(0.7, body.retention)))
            if body.archived is not None:
                deck.archived = body.archived
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.delete("/decks/{deck_id}")
    def delete_deck(request: Request, deck_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            db.query(StudyReview).filter(StudyReview.deck_id == deck.id).delete()
            db.query(StudyAttempt).filter(StudyAttempt.deck_id == deck.id).delete()
            db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck.id).delete()
            db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck.id).delete()
            db.delete(deck)  # cards cascade
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ------------------------------------------------------------------ cards

    @router.get("/decks/{deck_id}/cards")
    def list_cards(request: Request, deck_id: str, q: Optional[str] = None):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            query = db.query(StudyCard).filter(StudyCard.deck_id == deck.id)
            if user is not None:
                query = query.filter(StudyCard.owner == user)
            if q:
                like = f"%{q}%"
                query = query.filter(
                    StudyCard.front.ilike(like) | StudyCard.back.ilike(like))
            cards = query.order_by(StudyCard.created_at.desc()).all()
            return {"cards": [_card_to_dict(c) for c in cards]}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/cards")
    def create_cards(request: Request, deck_id: str, body: CardsCreate):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            created = []
            now = _utcnow_naive()
            for c in body.cards:
                if not c.front.strip() or not c.back.strip():
                    continue
                card = StudyCard(
                    id=str(uuid.uuid4()), owner=user, deck_id=deck.id,
                    front=c.front.strip(), back=c.back.strip(),
                    notes=c.notes,
                    tags=json.dumps(c.tags) if c.tags else None,
                    source=body.source or "user",
                    state="new", due=now,
                )
                db.add(card)
                created.append(card.id)
            db.commit()
            return {"created": len(created), "ids": created}
        finally:
            db.close()

    @router.put("/cards/{card_id}")
    def update_card(request: Request, card_id: str, body: CardUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            if body.front is not None:
                card.front = body.front.strip() or card.front
            if body.back is not None:
                card.back = body.back.strip() or card.back
            if body.notes is not None:
                card.notes = body.notes
            if body.tags is not None:
                card.tags = json.dumps(body.tags)
            if body.suspended is not None:
                card.suspended = body.suspended
            if body.deck_id is not None:
                study_service.get_deck(db, body.deck_id, user)  # ownership check
                card.deck_id = body.deck_id
            db.commit()
            return _card_to_dict(card)
        finally:
            db.close()

    @router.delete("/cards/{card_id}")
    def delete_card(request: Request, card_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            db.delete(card)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ------------------------------------------------------------------ queue + review

    @router.get("/queue")
    def review_queue(request: Request, deck_id: Optional[str] = None, limit: int = 60):
        """Due learning/relearning first, then due reviews, then capped new cards."""
        user = _owner(request)
        limit = max(1, min(200, limit))
        db = SessionLocal()
        try:
            now = _utcnow_naive()
            decks = ([study_service.get_deck(db, deck_id, user)] if deck_id else
                     db.query(StudyDeck).filter(
                         StudyDeck.archived == False,  # noqa: E712
                         *( [StudyDeck.owner == user] if user is not None else [] )
                     ).all())
            queue: List[Dict] = []
            for deck in decks:
                base = db.query(StudyCard).filter(
                    StudyCard.deck_id == deck.id,
                    StudyCard.suspended == False)  # noqa: E712
                if user is not None:
                    base = base.filter(StudyCard.owner == user)
                learning = base.filter(
                    StudyCard.state.in_(("learning", "relearning")),
                    StudyCard.due <= now).order_by(StudyCard.due.asc()).all()
                review = base.filter(
                    StudyCard.state == "review",
                    StudyCard.due <= now).order_by(StudyCard.due.asc()).all()
                cap = max(0, (deck.new_per_day or 0) - _new_introduced_today(db, user, deck.id))
                new = base.filter(StudyCard.state == "new") \
                    .order_by(StudyCard.created_at.asc()).limit(cap).all() if cap else []
                for c in learning + review + new:
                    queue.append(_card_to_dict(c, with_preview=True))
            # Default: already-seen cards (learning/review) sink behind new ones.
            # The per-user `study_order` pref = "review" restores the classic
            # spaced-repetition order (due reviews first, then new).
            if _read_pref(user, "study_order") == "review":
                phase_rank = {"learning": 0, "relearning": 0, "review": 1, "new": 2}
            else:
                phase_rank = {"new": 0, "learning": 1, "relearning": 1, "review": 2}
            queue.sort(key=lambda c: (phase_rank.get(c["state"], 3), c["due"] or ""))
            return {"queue": queue[:limit], "total": len(queue)}
        finally:
            db.close()

    @router.post("/cards/{card_id}/review")
    def review_card(request: Request, card_id: str, body: ReviewIn):
        user = _owner(request)
        if body.rating not in (1, 2, 3, 4):
            raise HTTPException(400, "rating must be 1-4")
        db = SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            deck = study_service.get_deck(db, card.deck_id, user)
            if body.idempotency_key:
                prior = db.query(StudyReview).filter(
                    StudyReview.owner == user,
                    StudyReview.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    return {
                        "card": _card_to_dict(card),
                        "interval_days": prior.interval_days,
                    }
            state_before = card.state or "new"
            user_w = _cached_w(db, user)
            result = fsrs.schedule(
                _card_fsrs_dict(card), body.rating,
                desired_retention=_flt(deck.retention, 0.9),
                w=(user_w if user_w is not None else fsrs.DEFAULT_W),
            )
            card.state = result["state"]
            card.stability = str(result["stability"])
            card.difficulty = str(result["difficulty"])
            card.due = _to_naive_utc(result["due"])
            card.last_review = _to_naive_utc(result["last_review"])
            card.reps = result["reps"]
            card.lapses = result["lapses"]
            try:
                db.add(StudyReview(
                    id=str(uuid.uuid4()), owner=user, card_id=card.id,
                    deck_id=card.deck_id, rating=body.rating,
                    state_before=state_before,
                    interval_days=result["interval_days"],
                    duration_ms=body.duration_ms,
                    reviewed_at=_utcnow_naive(),
                    idempotency_key=body.idempotency_key,
                ))
                db.commit()
            except IntegrityError:
                db.rollback()
                card = study_service.get_card(db, card_id, user)
                prior = db.query(StudyReview).filter(
                    StudyReview.owner == user,
                    StudyReview.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    return {
                        "card": _card_to_dict(card),
                        "interval_days": prior.interval_days,
                    }
                raise
            return {"card": _card_to_dict(card), "interval_days": result["interval_days"]}
        finally:
            db.close()

    # ------------------------------------------------------------------ AI

    @router.post("/ai/generate-cards")
    async def ai_generate_cards(request: Request, body: GenerateCardsIn):
        """Source text -> proposed cards. Returns proposals; nothing is saved."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        text = (body.text or "").strip()
        source = "text"
        if body.material_id:
            picked = card_source_text(user, body.material_id)
            text, source = picked["text"], picked["source"]
        if len(text) < 30:
            raise HTTPException(400, "Provide more source material (at least a paragraph).")
        count = max(1, min(40, body.count))
        focus = f"\nFocus especially on: {body.focus.strip()}" if body.focus else ""
        prompt = (f"Create about {count} flashcards from this material.{focus}\n\n"
                  f"--- MATERIAL ---\n{text[:24000]}")
        value = await _llm_json(user, CARD_AUTHOR_SYSTEM, prompt,
                                temperature=0.4, max_tokens=12000,
                                timeout=180, thinking_off=True)
        if isinstance(value, dict):
            value = value.get("cards") or [value]
        if not isinstance(value, list):
            raise HTTPException(502, "Model reply was not a card list. Try again.")
        cards = []
        for item in value:
            if isinstance(item, dict) and item.get("front") and item.get("back"):
                cards.append({"front": str(item["front"]).strip(),
                              "back": str(item["back"]).strip()})
        if not cards:
            raise HTTPException(502, "No usable cards in model reply. Try again.")
        return {"cards": cards}

    @router.post("/ai/quiz")
    async def ai_quiz(request: Request, body: QuizIn):
        """Free-recall quiz: from a deck (no LLM needed) or from pasted text (LLM)."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        count = max(1, min(20, body.count))
        if body.deck_id:
            db = SessionLocal()
            try:
                deck = study_service.get_deck(db, body.deck_id, user)
                q = db.query(StudyCard).filter(
                    StudyCard.deck_id == deck.id,
                    StudyCard.suspended == False)  # noqa: E712
                if user is not None:
                    q = q.filter(StudyCard.owner == user)
                # Prioritize due/lapsed cards — quiz the weak spots first.
                now = _utcnow_naive()
                due = q.filter(StudyCard.state != "new", StudyCard.due <= now) \
                    .order_by(StudyCard.due.asc()).limit(count).all()
                rest_needed = count - len(due)
                rest = []
                if rest_needed > 0:
                    exclude = [c.id for c in due]
                    rq = q
                    if exclude:
                        rq = rq.filter(~StudyCard.id.in_(exclude))
                    rest = rq.order_by(StudyCard.lapses.desc(),
                                       StudyCard.created_at.desc()) \
                        .limit(rest_needed).all()
                cards = due + rest
                if not cards:
                    raise HTTPException(400, "Deck has no cards to quiz from.")
                return {"questions": [
                    {"question": c.front, "reference": c.back, "card_id": c.id}
                    for c in cards
                ], "source": "deck"}
            finally:
                db.close()
        text = (body.text or "").strip()
        if len(text) < 30:
            raise HTTPException(400, "Pick a deck or paste source material.")
        prompt = (f"Write {count} free-recall questions from this material.\n\n"
                  f"--- MATERIAL ---\n{text[:24000]}")
        value = await _llm_json(user, QUIZ_AUTHOR_SYSTEM, prompt,
                                temperature=0.5, max_tokens=12000,
                                timeout=180, thinking_off=True)
        if isinstance(value, dict):
            value = value.get("questions") or [value]
        questions = []
        for item in (value if isinstance(value, list) else []):
            if isinstance(item, dict) and item.get("question") and item.get("reference"):
                questions.append({"question": str(item["question"]).strip(),
                                  "reference": str(item["reference"]).strip(),
                                  "card_id": None})
        if not questions:
            raise HTTPException(502, "No usable questions in model reply. Try again.")
        return {"questions": questions, "source": "ai"}

    @router.post("/ai/grade")
    async def ai_grade(request: Request, body: GradeIn):
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        if not body.answer.strip():
            return {"score": 0, "verdict": "incorrect",
                    "feedback": "No answer given. Attempt a recall before checking — "
                                "even a wrong attempt strengthens the memory more than peeking.",
                    "followup": None}
        prompt = (f"QUESTION:\n{body.question.strip()}\n\n"
                  f"REFERENCE ANSWER:\n{body.reference.strip()}\n\n"
                  f"LEARNER'S ANSWER:\n{body.answer.strip()[:8000]}")
        # Thinking stays ON for grading — careful rubric comparison benefits
        # from reasoning; the budget covers the hidden tokens plus the JSON.
        value = await _llm_json(user, GRADER_SYSTEM, prompt,
                                temperature=0.2, max_tokens=8000, timeout=120)
        if not isinstance(value, dict):
            raise HTTPException(502, "Model reply was not a grade object. Try again.")
        score = value.get("score")
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            score = 0
        verdict = value.get("verdict")
        if verdict not in ("correct", "partial", "incorrect"):
            verdict = "correct" if score >= 85 else ("partial" if score >= 40 else "incorrect")
        return {"score": score, "verdict": verdict,
                "feedback": str(value.get("feedback") or "").strip(),
                "followup": (str(value.get("followup")).strip()
                             if value.get("followup") else None)}

    # ------------------------------------------------------------------ exams / plans

    @router.get("/exams")
    def list_exams(request: Request, archived: bool = False):
        user = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(StudyExam).filter(StudyExam.archived == archived)
            if user is not None:
                q = q.filter(StudyExam.owner == user)
            exams = q.order_by(StudyExam.exam_date.asc()).all()
            return {"exams": [_exam_to_dict(e) for e in exams]}
        finally:
            db.close()

    @router.post("/exams")
    def create_exam(request: Request, body: ExamCreate):
        user = _owner(request)
        if not body.title.strip():
            raise HTTPException(400, "Title is required")
        try:
            date.fromisoformat(body.exam_date)
        except ValueError:
            raise HTTPException(400, "exam_date must be an ISO date (YYYY-MM-DD)")
        db = SessionLocal()
        try:
            exam = StudyExam(
                id=str(uuid.uuid4()), owner=user, title=body.title.strip(),
                exam_date=body.exam_date, exam_format=body.exam_format,
                hours_per_week=str(body.hours_per_week),
                rest_days=json.dumps(body.rest_days) if body.rest_days else None,
                topics=json.dumps(body.topics or []),
                deck_id=_vet_exam_deck(db, body.deck_id, user),
            )
            db.add(exam)
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.put("/exams/{exam_id}")
    def update_exam(request: Request, exam_id: str, body: ExamUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            if body.title is not None:
                exam.title = body.title.strip() or exam.title
            if body.exam_date is not None:
                try:
                    date.fromisoformat(body.exam_date)
                except ValueError:
                    raise HTTPException(400, "exam_date must be an ISO date")
                exam.exam_date = body.exam_date
            if body.exam_format is not None:
                exam.exam_format = body.exam_format
            if body.hours_per_week is not None:
                exam.hours_per_week = str(body.hours_per_week)
            if body.rest_days is not None:
                exam.rest_days = json.dumps(body.rest_days)
            if body.topics is not None:
                exam.topics = json.dumps(body.topics)
            if body.archived is not None:
                exam.archived = body.archived
            if body.deck_id is not None:
                exam.deck_id = _vet_exam_deck(db, body.deck_id, user)
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.delete("/exams/{exam_id}")
    def delete_exam(request: Request, exam_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            db.delete(exam)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/exams/{exam_id}/generate-plan")
    def generate_exam_plan(request: Request, exam_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            topics = json.loads(exam.topics) if exam.topics else []
            topic_names = [str(t.get("name") or "").strip() for t in topics]
            topic_names = [n for n in topic_names if n]

            # --- FSRS mastery injection ---
            mastery_scores: Dict[str, float] = {}
            if topic_names:
                # Gather cards for this user (no deck filter: topics may span decks).
                card_rows = db.query(StudyCard).filter(
                    StudyCard.owner == user,
                    StudyCard.suspended == False,
                ).all()
                card_dicts: List[Dict] = []
                for c in card_rows:
                    card_dicts.append(_card_to_dict(c))

                q_rows = db.query(StudyQuestion).filter(
                    StudyQuestion.owner == user,
                    StudyQuestion.suspended == False,
                ).all()
                q_dicts: List[Dict] = []
                for q in q_rows:
                    # use lightweight dict with only fields compute_mastery_scores needs
                    q_dicts.append({
                        "state": q.state or "new",
                        "stability": _flt(q.stability),
                        "topic": q.topic,
                    })
                mastery_scores = compute_mastery_scores(topic_names, card_dicts, q_dicts)

            try:
                plan = generate_plan(
                    date.fromisoformat(exam.exam_date),
                    topics,
                    hours_per_week=_flt(exam.hours_per_week, 7.0),
                    rest_days=json.loads(exam.rest_days) if exam.rest_days else None,
                    mastery_scores=mastery_scores or None,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            exam.plan = json.dumps(plan)

            # --- done_blocks preservation on regen ---
            old_done = json.loads(exam.done_blocks) if exam.done_blocks else []
            if isinstance(old_done, list):
                exam.done_blocks = json.dumps(migrate_done_blocks(old_done, plan))
            else:
                exam.done_blocks = json.dumps([])
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.post("/exams/{exam_id}/toggle-block")
    def toggle_plan_block(request: Request, exam_id: str, body: ToggleBlockIn):
        user = _owner(request)
        db = SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            done = set(json.loads(exam.done_blocks) if exam.done_blocks else [])
            if body.key in done:
                done.discard(body.key)
            else:
                done.add(body.key)
            exam.done_blocks = json.dumps(sorted(done))
            db.commit()
            return {"done_blocks": sorted(done)}
        finally:
            db.close()

    # ------------------------------------------------------------------ focus

    @router.post("/focus/start")
    def focus_start(request: Request, body: FocusStart):
        user = _owner(request)
        db = SessionLocal()
        try:
            s = StudyFocusSession(
                id=str(uuid.uuid4()), owner=user, label=body.label,
                planned_min=max(1, min(240, body.planned_min)),
                started_at=_utcnow_naive(),
            )
            db.add(s)
            db.commit()
            return _focus_to_dict(s)
        finally:
            db.close()

    @router.post("/focus/{session_id}/finish")
    def focus_finish(request: Request, session_id: str, body: FocusFinish):
        user = _owner(request)
        db = SessionLocal()
        try:
            s = db.query(StudyFocusSession).filter(StudyFocusSession.id == session_id).first()
            if not s or (user is not None and s.owner != user):
                raise HTTPException(404, "Focus session not found")
            s.actual_min = max(0, min(24 * 60, body.actual_min))
            s.completed = body.completed
            s.ended_at = _utcnow_naive()
            db.commit()
            return _focus_to_dict(s)
        finally:
            db.close()

    @router.get("/focus/recent")
    def focus_recent(request: Request, days: int = 14):
        user = _owner(request)
        days = max(1, min(90, days))
        db = SessionLocal()
        try:
            since = _utcnow_naive() - timedelta(days=days)
            q = db.query(StudyFocusSession).filter(StudyFocusSession.started_at >= since)
            if user is not None:
                q = q.filter(StudyFocusSession.owner == user)
            sessions = q.order_by(StudyFocusSession.started_at.desc()).all()
            return {"sessions": [_focus_to_dict(s) for s in sessions]}
        finally:
            db.close()

    # ------------------------------------------------------------------ materials (v2)

    @router.get("/decks/{deck_id}/materials")
    def list_materials(request: Request, deck_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            q = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck.id)
            if user is not None:
                q = q.filter(StudyMaterial.owner == user)
            return {"materials": [_material_to_dict(m) for m in
                                  q.order_by(StudyMaterial.created_at.desc()).all()]}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/materials")
    def create_material(request: Request, deck_id: str, body: MaterialCreate):
        """Attach source material: pasted text or a previously uploaded file."""
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            if body.file_id:
                kind = "pdf" if body.file_id.lower().endswith(".pdf") else "file"
                name = (body.name or body.file_id).strip()
                try:
                    text = _extract_file_text(body.file_id, user)
                except HTTPException as e:
                    # Scanned/image-only PDFs have no text layer - keep the
                    # material anyway; vision extraction reads the pages.
                    if kind == "pdf" and e.status_code == 422:
                        text = ""
                    else:
                        raise
            else:
                text = (body.text or "").strip()
                kind = "text"
                name = (body.name or "").strip() or (text[:48] + "…" if len(text) > 48 else text[:48])
            if len(text) < 30 and kind != "pdf":
                raise HTTPException(400, "Provide more material (at least a paragraph).")
            m = StudyMaterial(
                id=str(uuid.uuid4()), owner=user, deck_id=deck.id,
                name=name[:200], kind=kind, file_id=body.file_id,
                content=text, char_count=len(text),
                category=classify_material(name),   # auto-tag on upload; user can change it
            )
            db.add(m)
            db.commit()
            return _material_to_dict(m)
        finally:
            db.close()

    @router.delete("/materials/{material_id}")
    def delete_material(request: Request, material_id: str, with_questions: bool = False):
        user = _owner(request)
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            if with_questions:
                db.query(StudyQuestion).filter(StudyQuestion.material_id == m.id).delete()
            db.delete(m)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.put("/materials/{material_id}/category")
    def set_material_category(request: Request, material_id: str, body: MaterialCategoryIn):
        """Change a material's category (theory vs exam/answer-key). This drives
        whether Consult / Explain-further search it as a theory source."""
        if body.category not in MATERIAL_CATEGORIES:
            raise HTTPException(400, f"category must be one of {MATERIAL_CATEGORIES}")
        user = _owner(request)
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            m.category = body.category
            db.commit()
            return {"ok": True, "category": m.category}
        finally:
            db.close()

    @router.post("/materials/{material_id}/reextract-text")
    def reextract_material_text(request: Request, material_id: str):
        """Re-read a file-backed material's text in full.

        Materials uploaded before the 15k cap was lifted only stored the first
        ~15k chars. This re-extracts the whole file so notes/extraction see all
        of it. No-op for pasted-text materials (they were never truncated)."""
        user = _owner(request)
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            if not m.file_id:
                raise HTTPException(400, "This material is pasted text, not a file.")
            file_id = m.file_id
        finally:
            db.close()
        text = _extract_file_text(file_id, user)  # max_chars=None -> full text
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            before = m.char_count or 0
            m.content = text
            m.char_count = len(text)
            db.commit()
            return {"ok": True, "char_count": len(text), "previous": before}
        finally:
            db.close()

    @router.post("/materials/{material_id}/transcribe")
    async def transcribe_material(request: Request, material_id: str):
        """Vision OCR for scanned / formula PDFs (see run_transcribe_material)."""
        return await run_transcribe_material(_owner(request), material_id)

    # ------------------------------------------------------------ study notes

    @router.get("/figures/{material_id}/{idx}")
    def get_study_figure(request: Request, material_id: str, idx: int):
        """Serve one extracted figure image inline (embedded in study notes)."""
        import os
        from fastapi.responses import FileResponse
        user = _owner(request)
        db = SessionLocal()
        try:
            study_service.get_material(db, material_id, user)  # ownership check
        finally:
            db.close()
        path = os.path.join(_study_figures_dir(material_id), f"{int(idx)}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, "Figure not found")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"X-Content-Type-Options": "nosniff"},
                            content_disposition_type="inline")

    @router.get("/materials/{material_id}/notes")
    def get_material_notes(request: Request, material_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            return {"summary": m.summary or "", "name": m.name, "file_id": m.file_id}
        finally:
            db.close()

    @router.post("/materials/{material_id}/notes")
    async def generate_material_notes(request: Request, material_id: str):
        """Generate (or regenerate) consultable study notes for one material:
        a Markdown summary from the full text, plus a Key-figures section with
        figures pulled from the source PDF and cited to their page."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            content = (m.content or "").strip()
            name, file_id, kind = m.name, m.file_id, m.kind
        finally:
            db.close()
        if len(content) < 200:
            raise HTTPException(400, "Not enough text in this material to write "
                                     "notes. If it is a scanned PDF, run vision "
                                     "extraction or re-extract its text first.")
        notes = await _llm_text(
            user, STUDY_NOTES_SYSTEM,
            f"Material name: {name}\n\n--- MATERIAL ---\n{content[:120000]}",
            temperature=0.3, max_tokens=8000, timeout=240)

        figures_md = ""
        pdf_path = None
        if file_id and (kind == "pdf" or str(file_id).lower().endswith(".pdf")):
            try:
                pdf_path = _resolve_uploaded_file(file_id)
            except HTTPException:
                pdf_path = None
        if pdf_path:
            figures_md = await _build_figures_section(user, material_id, file_id, pdf_path)

        full = notes.strip() + figures_md
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            m.summary = full
            db.commit()
        finally:
            db.close()
        return {"summary": full, "has_figures": bool(figures_md)}

    @router.get("/decks/{deck_id}/overview")
    def get_deck_overview(request: Request, deck_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            return {"overview": deck.overview or ""}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/overview")
    async def generate_deck_overview(request: Request, deck_id: str):
        """Generate a short subject overview from the chapter notes (preferred)
        or raw material text, tying the chapters together."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            deck_name = deck.name
            q = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
            if user is not None:
                q = q.filter(StudyMaterial.owner == user)
            parts = []
            for m in q.order_by(StudyMaterial.created_at.asc()).all():
                src = (m.summary or m.content or "")[:4000].strip()
                if src:
                    parts.append(f"### {m.name}\n{src}")
        finally:
            db.close()
        if not parts:
            raise HTTPException(400, "Add materials (and ideally generate chapter "
                                     "notes) before generating a subject overview.")
        prompt = (f"Subject: {deck_name}\n\n" + "\n\n".join(parts))[:60000]
        overview = await _llm_text(user, SUBJECT_OVERVIEW_SYSTEM, prompt,
                                   temperature=0.3, max_tokens=4000, timeout=180)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            deck.overview = overview
            db.commit()
        finally:
            db.close()
        return {"overview": overview}

    @router.post("/materials/{material_id}/extract")
    async def extract_questions(request: Request, material_id: str, body: ExtractIn):
        """AI question extraction: material text -> saved question bank items.

        mode "extract": pull the actual questions out of past papers/problem
        sets, faithfully. mode "author": write new exam-style questions from
        notes. Long materials are chunked; partial results are kept (JSON
        repair recovers complete objects from malformed replies).
        """
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        mode = body.mode if body.mode in ("extract", "author") else "extract"
        types = [t for t in (body.types or ["mcq", "open"]) if t in ("mcq", "open")] or ["mcq", "open"]
        db = SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            deck_id = m.deck_id
            content = m.content or ""
            file_id = m.file_id
            kind = m.kind
        finally:
            db.close()

        # Locate the original PDF (vision mode and the auto-fallback need it).
        pdf_path = None
        if file_id and (kind == "pdf" or str(file_id).lower().endswith(".pdf")):
            try:
                pdf_path = _resolve_uploaded_file(file_id)
            except HTTPException:
                pdf_path = None

        # Thin text layer (formula images / scans): go vision-first instead of
        # wasting a text pass on cover-page scraps.
        auto_vision = False
        if pdf_path and not body.vision:
            try:
                from src.study_vision import pdf_page_count, text_layer_is_thin
                auto_vision = text_layer_is_thin(len(content), pdf_page_count(pdf_path))
            except Exception:
                auto_vision = False

        system = EXTRACT_QUESTIONS_SYSTEM if mode == "extract" else AUTHOR_QUESTIONS_SYSTEM
        type_note = ("Only produce questions of type: " + ", ".join(types) + ".") \
            if len(types) == 1 else ""

        async def _run_text_pass(text_chunks: List[str]) -> tuple:
            """Per-chunk text extraction. Returns (collected, raw_count, errors)."""
            per_chunk = (max(3, min(40, body.count) // len(text_chunks) + 1)
                         if mode == "author" else None)
            t_collected: List[Dict] = []
            t_raw = 0
            t_errors = 0
            for i, chunk in enumerate(text_chunks):
                if mode == "author":
                    instruction = (f"Write about {per_chunk} questions from this material "
                                   f"(part {i + 1}/{len(text_chunks)}). {type_note}")
                else:
                    instruction = (f"Extract every practice question from this material "
                                   f"(part {i + 1}/{len(text_chunks)}). {type_note}")
                value = None
                for attempt in range(2):
                    strict = "" if attempt == 0 else (
                        "\n\nIMPORTANT: your previous reply was not valid JSON. Reply with "
                        "ONLY the JSON array - it must start with [ and end with ]. "
                        "No prose, no markdown, no explanations.")
                    try:
                        value = await _llm_json(user, system,
                                                f"{instruction}{strict}\n\n--- MATERIAL ---\n{chunk}",
                                                temperature=0.2 if mode == "extract" else 0.5,
                                                max_tokens=EXTRACTION_MAX_TOKENS,
                                                timeout=300, thinking_off=True)
                        break
                    except HTTPException as e:
                        if e.status_code == 503:
                            raise  # no model configured — fail loudly, not partially
                        logger.warning("study extract: chunk %d attempt %d failed: %s",
                                       i, attempt + 1, e.detail)
                        if attempt == 1:
                            t_errors += 1
                if value is not None:
                    if isinstance(value, list):
                        t_raw += len(value)
                    elif isinstance(value, dict):
                        t_raw += len(value.get("questions") or [value])
                    t_collected.extend(normalize_questions(value))
            return t_collected, t_raw, t_errors

        def _finalize(items: List[Dict]) -> List[Dict]:
            """Type-filter, de-duplicate, and (in extract mode) drop conclusion-
            style 'questions' that leak their own answer."""
            qs = dedupe_questions([q for q in items if q["qtype"] in types])
            if mode == "extract":
                qs = [q for q in qs if not question_is_conclusion(q)]
            return qs

        used_vision = False
        coverage = None
        if body.vision or auto_vision:
            if not pdf_path:
                raise HTTPException(400, "Vision extraction needs the original PDF "
                                         "file. Re-upload the PDF to this subject.")
            collected, raw_count, n_batches, errors, coverage = \
                await _extract_questions_vision(user, mode, types, pdf_path)
            used_vision = True
            chunks = [None] * n_batches  # for the response chunk count
        else:
            chunks = chunk_material(content)
            if not chunks and pdf_path:
                # No text layer at all - skip straight to vision.
                collected, raw_count, n_batches, errors, coverage = \
                    await _extract_questions_vision(user, mode, types, pdf_path)
                used_vision = True
                chunks = [None] * n_batches
            elif not chunks:
                raise HTTPException(400, "Material has no text to extract from.")
            else:
                collected, raw_count, errors = await _run_text_pass(chunks)
                # Coverage pass (text): compare against a discovery manifest
                # and re-request anything missed in one targeted call.
                if mode == "extract" and collected:
                    manifest = await _discover_questions_text(user, content)
                    page_by_number = _manifest_page_map(manifest)
                    _attach_source_pages(collected, page_by_number)
                    coverage = _coverage_report(manifest, collected)
                    if coverage and coverage["missing"]:
                        nums = ", ".join(coverage["missing"])
                        logger.info("study coverage: re-requesting question(s) %s "
                                    "(text)", nums)
                        try:
                            value = await _llm_json(
                                user, system,
                                f"A previous pass missed some questions. Extract "
                                f"ONLY question(s) {nums} from this material, "
                                f"faithfully and completely. {type_note}\n\n"
                                f"--- MATERIAL ---\n{content[:40000]}",
                                temperature=0.2, max_tokens=EXTRACTION_MAX_TOKENS,
                                timeout=300, thinking_off=True)
                            fresh = normalize_questions(value)
                            _attach_source_pages(fresh, page_by_number)
                            raw_count += len(fresh)
                            collected.extend(fresh)
                            coverage = _coverage_report(manifest, collected)
                        except HTTPException as e:
                            if e.status_code == 503:
                                raise
                            logger.warning("study coverage: targeted text pass "
                                           "failed: %s", e.detail)

        questions = _finalize(collected)

        # Auto-fallback: text extraction found nothing usable but we have the
        # original PDF — its text layer is probably thin (formula images,
        # scans). Try vision before giving up.
        if not questions and not used_vision and pdf_path:
            logger.info("study extract: text pass empty for material %s — "
                        "falling back to vision extraction", material_id)
            try:
                v_collected, v_raw, v_batches, v_errors, v_coverage = \
                    await _extract_questions_vision(user, mode, types, pdf_path)
                if v_collected:
                    collected, raw_count, errors = v_collected, v_raw, v_errors
                    chunks = [None] * v_batches
                    used_vision = True
                    coverage = v_coverage
                    questions = _finalize(collected)
            except HTTPException as e:
                logger.warning("study extract: vision fallback unavailable: %s", e.detail)

        # Mirror fallback: vision found nothing usable but the material HAS a
        # text layer (e.g. the vision model can't read images, or the user hit
        # "Extract (vision)" on a text-rich PDF). Try text before giving up.
        if not questions and used_vision:
            text_chunks = chunk_material(content)
            if text_chunks:
                logger.info("study extract: vision pass empty for material %s — "
                            "falling back to text extraction", material_id)
                try:
                    t_collected, t_raw, t_errors = await _run_text_pass(text_chunks)
                    if t_collected:
                        collected, raw_count, errors = t_collected, t_raw, t_errors
                        chunks = text_chunks
                        used_vision = False
                        coverage = None  # manifest came from the failed pass
                        questions = _finalize(collected)
                except HTTPException as e:
                    logger.warning("study extract: text fallback failed: %s", e.detail)

        if mode == "extract" and questions:
            for q in questions:
                if not q.get("source_page"):
                    page = infer_source_page(
                        content,
                        number=q.get("number"),
                        question=q.get("question"),
                    )
                    if page:
                        q["source_page"] = page

        if not questions:
            if errors >= len(chunks):
                detail = ("Every chunk failed: the model's replies were empty or "
                          "not parseable as JSON (the server log has the raw "
                          "replies). Empty replies usually mean the reply was "
                          "truncated mid-reasoning; retry, or switch the Study "
                          "model (the model selector in the top bar).")
            elif raw_count == 0:
                detail = ("The model returned valid JSON but found no questions in "
                          "this material. If it is notes rather than an exam, use "
                          "'Author questions' instead of 'Extract questions'.")
            elif types != ["mcq", "open"]:
                detail = (f"{raw_count} question(s) were found but none matched the "
                          f"requested type filter ({', '.join(types)}).")
            else:
                detail = (f"The model found {raw_count} question(s) but none were "
                          "usable (e.g. MCQs whose correct answer could not be "
                          "identified). Try again or switch the Study model.")
            raise HTTPException(502, detail)

        db = SessionLocal()
        try:
            now = _utcnow_naive()
            # Cross-run dedupe: never re-add a question already in this deck, so
            # re-running extraction (or extracting overlapping materials) tops up
            # the bank instead of duplicating it.
            existing_keys = {
                question_key(text) for (text,) in
                db.query(StudyQuestion.question)
                  .filter(StudyQuestion.deck_id == deck_id).all()
            }
            saved = []
            duplicates = 0
            for q in questions:
                key = question_key(q["question"])
                if key in existing_keys:
                    duplicates += 1
                    continue
                existing_keys.add(key)
                row = StudyQuestion(
                    id=str(uuid.uuid4()), owner=user, deck_id=deck_id,
                    material_id=material_id, qtype=q["qtype"],
                    question=q["question"],
                    context=q.get("context"),
                    options=json.dumps(q["options"]) if q["options"] else None,
                    correct_index=q["correct_index"], reference=q["reference"],
                    topic=q["topic"], difficulty=q["difficulty"],
                    number=q.get("number"),
                    source_page=q.get("source_page"),
                    origin="extracted" if mode == "extract" else "authored",
                    state="new", due=now,
                )
                db.add(row)
                saved.append(row)
            m = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
            if m:
                m.question_count = (m.question_count or 0) + len(saved)
            db.commit()
            original_links = _question_original_links(db, saved)
            resp = {
                "created": len(saved),
                "duplicates": duplicates,
                "chunks": len(chunks),
                "chunk_errors": errors,
                "vision": used_vision,
                "coverage": coverage,
                "questions": [_question_to_dict(r, original=original_links.get(r.id))
                              for r in saved],
            }
            created_n = len(saved)
        finally:
            db.close()

        # Auto-link multi-part problems for this material so practice immediately
        # shows earlier parts + answers as context. Best-effort — an extraction
        # must never fail because grouping did.
        if created_n:
            try:
                await _link_deck_parts(user, deck_id, only_material=material_id)
            except Exception as e:
                logger.warning("study: auto link-parts after extraction failed: %s", e)
        return resp

    # ------------------------------------------------------------------ question bank (v2)

    @router.get("/decks/{deck_id}/questions")
    def list_questions(request: Request, deck_id: str, q: Optional[str] = None,
                       qtype: Optional[str] = None, material_id: Optional[str] = None):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            query = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck.id)
            if user is not None:
                query = query.filter(StudyQuestion.owner == user)
            if material_id:
                query = query.filter(StudyQuestion.material_id == material_id)
            if qtype in ("mcq", "open"):
                query = query.filter(StudyQuestion.qtype == qtype)
            if q:
                query = query.filter(StudyQuestion.question.ilike(f"%{q}%"))
            rows = query.order_by(StudyQuestion.created_at.desc()).all()
            original_links = _question_original_links(db, rows)
            return {"questions": [_question_to_dict(r, original=original_links.get(r.id))
                                  for r in rows]}
        finally:
            db.close()

    @router.put("/questions/{question_id}")
    def update_question(request: Request, question_id: str, body: QuestionUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if body.question is not None:
                row.question = body.question.strip() or row.question
            if body.options is not None:
                opts = [o.strip() for o in body.options if o.strip()]
                if row.qtype == "mcq" and len(opts) < 2:
                    raise HTTPException(400, "MCQ needs at least 2 options")
                row.options = json.dumps(opts) if opts else None
            if body.correct_index is not None:
                opts = json.loads(row.options) if row.options else []
                if not (0 <= body.correct_index < len(opts)):
                    raise HTTPException(400, "correct_index out of range")
                row.correct_index = body.correct_index
            if body.reference is not None:
                row.reference = body.reference
            if body.topic is not None:
                row.topic = body.topic.strip() or None
            if body.difficulty in ("easy", "medium", "hard"):
                row.difficulty = body.difficulty
            if body.suspended is not None:
                row.suspended = body.suspended
            row.explanation = None if body.options is not None or body.reference is not None \
                else row.explanation
            db.commit()
            material = db.query(StudyMaterial).filter(StudyMaterial.id == row.material_id).first() \
                if row.material_id else None
            return _question_to_dict(row, original=_question_original_link(row, material))
        finally:
            db.close()

    @router.delete("/questions/{question_id}")
    def delete_question(request: Request, question_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            db.query(StudyAttempt).filter(StudyAttempt.question_id == row.id).delete()
            db.delete(row)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ------------------------------------------------------------------ practice (v2)

    @router.get("/practice/queue")
    def practice_queue(request: Request, deck_id: Optional[str] = None,
                       limit: int = 20, mode: Optional[str] = None,
                       adaptive: bool = False):
        """Due questions first (spaced retrieval), then new ones interleaved
        across topics (round-robin) instead of blocked by topic.

        When ``mode=pretest`` is set, one *unseen* (new, reps==0) question per
        topic is lifted ahead of the rest as a pretest item (errorful-generation
        effect, d~0.35).  Pretest items are marked with ``"pretest": true`` in
        the response so the frontend can label them.  No new schema, no DB
        writes — pure queue ordering over existing questions."""
        user = _owner(request)
        limit = max(1, min(100, limit))
        db = SessionLocal()
        try:
            now = _utcnow_naive()
            base = db.query(StudyQuestion).filter(
                StudyQuestion.suspended == False)  # noqa: E712
            if deck_id:
                study_service.get_deck(db, deck_id, user)
                base = base.filter(StudyQuestion.deck_id == deck_id)
            if user is not None:
                base = base.filter(StudyQuestion.owner == user)
            due = base.filter(StudyQuestion.state != "new",
                              StudyQuestion.due <= now) \
                .order_by(StudyQuestion.due.asc()).limit(limit).all()
            new_rows = base.filter(StudyQuestion.state == "new") \
                .order_by(StudyQuestion.created_at.asc()).limit(limit * 3).all()
            # Interleave new questions across topics: round-robin over topic groups.
            groups: Dict[str, List[StudyQuestion]] = {}
            for r in new_rows:
                groups.setdefault(r.topic or "general", []).append(r)
            interleaved: List[StudyQuestion] = []
            while groups and len(interleaved) < limit:
                for key in list(groups.keys()):
                    if groups[key]:
                        interleaved.append(groups[key].pop(0))
                    if not groups[key]:
                        del groups[key]
            # Ordering mode (per-user pref `study_order`):
            #   default / "completed" → already-answered (due/review) questions
            #                 sink behind every new/unseen one (clear new first).
            #   "review"  → due reviews first (immediate spaced retrieval).
            if _read_pref(user, "study_order") == "review":
                queue = due + interleaved
            else:
                queue = interleaved + due
            # --- adaptive weak-area weighting (Phase 2.4) ---
            weak_area_weights: Optional[List[Dict]] = None
            if adaptive:
                since = now - timedelta(days=42)
                candidate_ids = [r.id for r in queue]
                signals = get_weak_question_signals(db, user, since, question_ids=candidate_ids)
                stab_map = get_question_stability_signal(db, user, question_ids=candidate_ids)
                queue = _adaptive_question_priority(
                    queue,
                    signals["by_question"],
                    signals["by_topic"],
                    stab_map,
                )
                rows = queue[:limit]
                # expose transparent weights for returned rows
                weak_area_weights = []
                for r in rows:
                    qid = r.id
                    topic = getattr(r, "topic", None) or "general"
                    weak_area_weights.append({
                        "question_id": qid,
                        "topic": topic,
                        "accuracy": signals["by_question"].get(qid, {}).get("accuracy"),
                        "topic_accuracy": signals["by_topic"].get(topic, {}).get("accuracy"),
                        "stability": stab_map.get(qid),
                    })
            else:
                rows = queue[:limit]
                weak_area_weights = None
            pretest_ids: set = set()
            if mode == "pretest":
                # Pick one new, truly-unseen (reps==0) question per topic.
                # Exclude already-seen (reps>0) and non-new (due, review, etc).
                unseen: Dict[str, List[StudyQuestion]] = {}
                for r in new_rows:
                    if (r.state == "new") and (r.reps == 0):
                        unseen.setdefault(r.topic or "general", []).append(r)
                pretest: List[StudyQuestion] = []
                for _topic, items in unseen.items():
                    # oldest first (smallest created_at) for stability
                    items.sort(key=lambda x: x.created_at or now)
                    pick = items[0]
                    pretest.append(pick)
                # Remove pretest picks from rows so they don't duplicate.
                pretest_ids = {r.id for r in pretest}
                rows = [r for r in rows if r.id not in pretest_ids]
                # Pretests always come first, then the normal queue order.
                rows = pretest + rows
                rows = rows[:limit]
            original_links = _question_original_links(db, rows)
            out = []
            for r in rows:
                d = _question_to_dict(
                    r,
                    with_answer=False,
                    original=original_links.get(r.id),
                )
                if mode == "pretest" and r.id in pretest_ids:
                    d["pretest"] = True
                out.append(d)
            # pretest count = how many of the *limited* rows are pretests
            actual_pretest = sum(1 for r in rows if r.id in pretest_ids)
            resp: Dict[str, Any] = {
                "queue": out,
                "due": len(due),
                "total": len(queue),
                "pretest": actual_pretest,
            }
            if adaptive:
                resp["adaptive_weights"] = weak_area_weights
            return resp
        finally:
            db.close()

    @router.post("/questions/{question_id}/attempt")
    async def attempt_question(request: Request, question_id: str, body: AttemptIn):
        """Submit an answer. MCQ is checked locally; open answers are AI-graded.
        The outcome maps to an FSRS rating so practice is spaced automatically."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            qtype = row.qtype
            options = json.loads(row.options) if row.options else []
            question_text = row.question
            reference = row.reference or ""
            correct_index = row.correct_index
            if body.idempotency_key:
                prior = db.query(StudyAttempt).filter(
                    StudyAttempt.owner == user,
                    StudyAttempt.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    return {
                        "qtype": prior.qtype,
                        "correct": prior.correct,
                        "score": prior.score,
                        "grading": json.loads(prior.grading) if prior.grading else None,
                        "correct_index": correct_index if prior.qtype == "mcq" else None,
                        "reference": reference,
                        "rating": prior.rating,
                        "interval_days": None,
                        "next_due": _iso(row.due),
                    }
        finally:
            db.close()

        correct = None
        score = None
        grading = None
        answer_text = ""
        if qtype == "mcq":
            if body.choice_index is None or not (0 <= body.choice_index < len(options)):
                raise HTTPException(400, "choice_index required for MCQ")
            correct = (body.choice_index == correct_index)
            answer_text = options[body.choice_index]
        else:
            answer_text = (body.answer or "").strip()
            if not answer_text:
                grading = {"score": 0, "verdict": "incorrect",
                           "feedback": "No answer given. Attempt the recall before "
                                       "checking — even a failed attempt strengthens "
                                       "the memory more than peeking.",
                           "followup": None}
                score = 0
            else:
                ref_block = reference if reference.strip() else (
                    "(no reference available - first work out the correct answer "
                    "yourself, then grade the learner's answer against it)")
                prompt = (f"QUESTION:\n{question_text}\n\n"
                          f"REFERENCE ANSWER:\n{ref_block}\n\n"
                          f"LEARNER'S ANSWER:\n{answer_text[:8000]}")
                value = await _llm_json(user, GRADE_OPEN_SYSTEM, prompt,
                                        temperature=0.2, max_tokens=8000, timeout=180)
                if not isinstance(value, dict):
                    raise HTTPException(502, "Model grade was unparseable. Try again.")
                try:
                    score = max(0, min(100, int(value.get("score"))))
                except (TypeError, ValueError):
                    score = 0
                verdict = value.get("verdict")
                if verdict not in ("correct", "partial", "incorrect"):
                    verdict = "correct" if score >= 85 else ("partial" if score >= 40 else "incorrect")
                grading = {"score": score, "verdict": verdict,
                           "feedback": str(value.get("feedback") or "").strip(),
                           "followup": (str(value.get("followup")).strip()
                                        if value.get("followup") else None)}

        confidence = confidence_to_numeric(body.confidence)
        rating = rating_from_outcome(qtype, correct=correct, score=score,
                                     hints_used=body.hints_used or 0,
                                     confidence=confidence)

        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if body.idempotency_key:
                prior = db.query(StudyAttempt).filter(
                    StudyAttempt.owner == user,
                    StudyAttempt.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    return {
                        "qtype": prior.qtype,
                        "correct": prior.correct,
                        "score": prior.score,
                        "grading": json.loads(prior.grading) if prior.grading else None,
                        "correct_index": correct_index if prior.qtype == "mcq" else None,
                        "reference": reference,
                        "rating": prior.rating,
                        "interval_days": None,
                        "next_due": _iso(row.due),
                    }
            user_w = _cached_w(db, user)
            result = fsrs.schedule({
                "state": row.state or "new",
                "stability": _flt(row.stability),
                "difficulty": _flt(row.fsrs_difficulty),
                "last_review": row.last_review,
                "reps": row.reps or 0,
                "lapses": row.lapses or 0,
            }, rating, w=(user_w if user_w is not None else fsrs.DEFAULT_W))
            row.state = result["state"]
            row.stability = str(result["stability"])
            row.fsrs_difficulty = str(result["difficulty"])
            row.due = _to_naive_utc(result["due"])
            row.last_review = _to_naive_utc(result["last_review"])
            row.reps = result["reps"]
            row.lapses = result["lapses"]
            try:
                db.add(StudyAttempt(
                    id=str(uuid.uuid4()), owner=user, question_id=row.id,
                    deck_id=row.deck_id, qtype=qtype, answer=answer_text[:4000],
                    correct=correct, score=score, rating=rating,
                    confidence=confidence, hints_used=body.hints_used or 0,
                    grading=json.dumps(grading) if grading else None,
                    duration_ms=body.duration_ms, attempted_at=_utcnow_naive(),
                    idempotency_key=body.idempotency_key,
                ))
                db.commit()
            except IntegrityError:
                db.rollback()
                row = study_service.get_question(db, question_id, user)
                prior = db.query(StudyAttempt).filter(
                    StudyAttempt.owner == user,
                    StudyAttempt.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    prior_grading = json.loads(prior.grading) if prior.grading else None
                    return {
                        "qtype": prior.qtype,
                        "correct": prior.correct,
                        "score": prior.score,
                        "grading": prior_grading,
                        "correct_index": correct_index if prior.qtype == "mcq" else None,
                        "reference": reference,
                        "rating": prior.rating,
                        "interval_days": None,
                        "next_due": _iso(row.due),
                    }
                raise
            return {
                "qtype": qtype,
                "correct": correct,
                "score": score,
                "grading": grading,
                "correct_index": correct_index if qtype == "mcq" else None,
                "reference": reference,
                "rating": rating,
                "interval_days": result["interval_days"],
                "next_due": _iso(row.due),
            }
        finally:
            db.close()

    @router.post("/questions/{question_id}/hint")
    async def question_hint(request: Request, question_id: str, body: HintIn):
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        level = max(1, min(3, body.level))
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            question_text = row.question
            options = json.loads(row.options) if row.options else None
            reference = row.reference or ""
        finally:
            db.close()
        opts_txt = ("\nOPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options))) \
            if options else ""
        prompt = (f"HINT LEVEL: {level}\n\nQUESTION:\n{question_text}{opts_txt}\n\n"
                  f"REFERENCE SOLUTION (for your eyes only — do NOT reveal it):\n{reference}")
        hint = await _llm_text(user, HINT_SYSTEM, prompt,
                               temperature=0.3, max_tokens=4000, timeout=120)
        return {"level": level, "hint": hint}

    @router.post("/questions/{question_id}/ask")
    async def question_ask(request: Request, question_id: str, body: AskIn):
        """Conversational 'Ask AI' for a practice question. Before the student
        submits (answered=False) it runs in Socratic COACH mode — guidance/hints
        only, never the answer. After they submit it runs in TUTOR mode — full
        explanation. Grounded in the question + reference (for-eyes-only while
        coaching)."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        msg = (body.message or "").strip()
        if not msg:
            raise HTTPException(400, "Empty message")
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            question_text = row.question
            ctx = (row.context or "").strip()
            options = json.loads(row.options) if row.options else None
            reference = row.reference or ""
            correct_index = row.correct_index
        finally:
            db.close()

        opts_txt = ("\nOPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options))) if options else ""
        ctx_txt = f"\nPROBLEM SETUP:\n{ctx}" if ctx else ""
        draft = (body.draft or "").strip()
        if body.answered:
            if body.elaborate:
                system = ASK_ELABORATE_SYSTEM
                mode = "elaborate"
            else:
                system = ASK_TUTOR_SYSTEM
                mode = "tutor"
            ans = ""
            if options is not None and correct_index is not None:
                ans += f"\nCORRECT OPTION INDEX: {correct_index}"
            if reference:
                ans += f"\nREFERENCE SOLUTION:\n{reference}"
            if draft:
                ans += f"\n\nSTUDENT'S SUBMITTED ANSWER:\n{draft}"
        else:
            system = ASK_COACH_SYSTEM
            mode = "coach"
            ans = f"\nREFERENCE SOLUTION (FOR YOUR EYES ONLY — never reveal):\n{reference}" if reference else ""
            if draft:
                ans += f"\n\nStudent's current draft (NOT submitted):\n{draft}"

        convo = ""
        for t in (body.history or [])[-12:]:
            if not isinstance(t, dict):
                continue
            who = "Student" if t.get("role") == "student" else "AI"
            convo += f"{who}: {str(t.get('content', '')).strip()}\n"

        prompt = (f"QUESTION:\n{question_text}{opts_txt}{ctx_txt}{ans}\n\n"
                  f"CONVERSATION SO FAR:\n{convo}Student: {msg}\n\n"
                  f"Reply to the student's latest message.")
        reply = await _llm_text(user, system, prompt,
                                temperature=0.3, max_tokens=4000, timeout=120)
        return {"reply": reply, "mode": mode}

    @router.post("/questions/{question_id}/explain")
    async def question_explain(request: Request, question_id: str):
        """Post-attempt explanation for an MCQ (cached on the question)."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if row.explanation:
                return {"explanation": row.explanation, "cached": True}
            if row.qtype != "mcq":
                return {"explanation": row.reference or "", "cached": False}
            question_text = row.question
            options = json.loads(row.options) if row.options else []
            correct_index = row.correct_index
            reference = row.reference or ""
        finally:
            db.close()
        opts_txt = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
        prompt = (f"QUESTION:\n{question_text}\n\nOPTIONS:\n{opts_txt}\n\n"
                  f"CORRECT OPTION INDEX: {correct_index}\n"
                  f"REFERENCE NOTE: {reference}")
        explanation = await _llm_text(user, EXPLAIN_SYSTEM, prompt,
                                      temperature=0.2, max_tokens=6000, timeout=120)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            row.explanation = explanation
            db.commit()
        finally:
            db.close()
        return {"explanation": explanation, "cached": False}

    @router.post("/questions/{question_id}/explain-further")
    async def question_explain_further(request: Request, question_id: str,
                                       refresh: bool = False):
        """Material-grounded theory for a question + where to review it.
        Cached on the question; ?refresh=1 regenerates."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if row.deep_explanation and not refresh:
                return {"explanation": row.deep_explanation, "cached": True}
            q_text, options = row.question, json.loads(row.options) if row.options else None
            reference = row.reference or ""
            deck_id = row.deck_id
            own_material_id = row.material_id
            deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
            subject_name = deck.name if deck else "this subject"
            # Search the WHOLE subject — theory lives in the lecture files, not
            # the practice exam this question was extracted from.
            blocks, by_id = _deck_material_context(db, deck_id, user, theory_only=True)
        finally:
            db.close()

        prompt_parts = [f"QUESTION:\n{q_text}"]
        if options:
            prompt_parts.append("OPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options)))
        if reference:
            prompt_parts.append(f"ANSWER / REFERENCE:\n{reference}")
        if own_material_id and own_material_id in by_id:
            prompt_parts.append(f"(This question was extracted from MATERIAL "
                                f"{own_material_id} — likely a practice exam, not the theory source.)")
        notes_blocks = [f"### {by_id[mid]['name']}\n{by_id[mid]['summary'][:6000]}"
                        for mid in by_id if by_id[mid].get("summary")]
        if notes_blocks:
            prompt_parts.append("--- AI STUDY NOTES (by chapter) ---\n"
                                + "\n\n".join(notes_blocks)[:24000])
        prompt_parts.append("--- SUBJECT MATERIALS ---\n" + ("\n\n".join(blocks)
                            if blocks else "(no source materials available — explain from general theory)"))
        value = await _llm_json(user, EXPLAIN_FURTHER_SYSTEM, "\n\n".join(prompt_parts),
                                temperature=0.3, max_tokens=8000, timeout=240,
                                thinking_off=True)
        if not isinstance(value, dict):
            raise HTTPException(502, "The model reply was not usable. Try again.")
        md = _explain_further_markdown(value, by_id)
        if not _resolve_locations(value, by_id):
            md = await _append_web_theory(user, md, q_text, subject_name)
        if not md:
            raise HTTPException(502, "The model did not return an explanation. Try again.")
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            row.deep_explanation = md
            db.commit()
        finally:
            db.close()
        return {"explanation": md, "cached": False}

    @router.post("/questions/{question_id}/locate")
    async def question_locate(request: Request, question_id: str):
        """Consult: find which of the subject's files (and page) hold the content
        needed to answer this question. Returns {locations:[{file_id,name,page,
        label,url}]}; when nothing covers it, generates a hint instead so the
        learner isn't left empty-handed: {locations:[], hint:"..."}."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            q_text = row.question
            options = json.loads(row.options) if row.options else None
            reference = row.reference or ""
            deck_id = row.deck_id
            deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
            subject_name = deck.name if deck else "this subject"
            theory_blocks, by_id = _deck_material_context(db, deck_id, user, theory_only=True)
            all_blocks, _ = _deck_material_context(db, deck_id, user, theory_only=False)
        finally:
            db.close()

        opts_line = ("\nOPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options))) \
            if options else ""

        async def _locate(blocks):
            if not blocks:
                return []
            prompt = (f"QUESTION:\n{q_text}{opts_line}\n\n--- SUBJECT MATERIALS ---\n"
                      + "\n\n".join(blocks))
            try:
                value = await _llm_json(user, LOCATE_MATERIAL_SYSTEM, prompt,
                                        temperature=0.2, max_tokens=4000,
                                        timeout=180, thinking_off=True)
                return _resolve_locations(value, by_id) if isinstance(value, dict) else []
            except HTTPException as e:
                if e.status_code == 503:
                    raise
                logger.warning("study locate: %s", e.detail)
                return []

        # Escalate: theory files first, then all files (exercises may hold it).
        locations = await _locate(theory_blocks)
        if not locations and len(all_blocks) > len(theory_blocks):
            locations = await _locate(all_blocks)
        if locations:
            return {"locations": locations, "hint": None, "source": "material"}

        # No course material covers it → search the web for the theory.
        web_md, web_links = await _web_theory(user, f"{q_text}{opts_line}", subject_name)
        if web_links or web_md:
            return {"locations": web_links, "hint": web_md or None, "source": "web"}

        # Web unavailable too — fall back to a plain hint.
        hint_prompt = (f"HINT LEVEL: 1\n\nQUESTION:\n{q_text}{opts_line}\n\n"
                       f"REFERENCE SOLUTION (for your eyes only — do NOT reveal it):\n{reference}")
        try:
            hint = await _llm_text(user, HINT_SYSTEM, hint_prompt,
                                   temperature=0.3, max_tokens=4000, timeout=120)
        except HTTPException:
            hint = None
        return {"locations": [], "hint": hint, "source": "hint"}

    @router.post("/cards/{card_id}/explain-further")
    async def card_explain_further(request: Request, card_id: str,
                                   refresh: bool = False):
        """Material-grounded theory for a flashcard. Searches all of the deck's
        materials (theory lives in the lecture files). Cached; ?refresh=1 regenerates."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            if card.deep_explanation and not refresh:
                return {"explanation": card.deep_explanation, "cached": True}
            front, back, notes = card.front, card.back, card.notes or ""
            deck_id = card.deck_id
            deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
            subject_name = deck.name if deck else "this subject"
            blocks, by_id = _deck_material_context(db, deck_id, user, theory_only=True)
        finally:
            db.close()

        prompt_parts = [f"FLASHCARD FRONT:\n{front}", f"FLASHCARD BACK (answer):\n{back}"]
        if notes:
            prompt_parts.append(f"CARD NOTES:\n{notes}")
        notes_blocks = [f"### {by_id[mid]['name']}\n{by_id[mid]['summary'][:6000]}"
                        for mid in by_id if by_id[mid].get("summary")]
        if notes_blocks:
            prompt_parts.append("--- AI STUDY NOTES (by chapter) ---\n"
                                + "\n\n".join(notes_blocks)[:24000])
        prompt_parts.append("--- SUBJECT MATERIALS ---\n" + ("\n\n".join(blocks)
                            if blocks else "(no source materials available — explain from general theory)"))
        value = await _llm_json(user, EXPLAIN_FURTHER_SYSTEM, "\n\n".join(prompt_parts),
                                temperature=0.3, max_tokens=8000, timeout=240,
                                thinking_off=True)
        if not isinstance(value, dict):
            raise HTTPException(502, "The model reply was not usable. Try again.")
        md = _explain_further_markdown(value, by_id)
        if not _resolve_locations(value, by_id):
            md = await _append_web_theory(user, md, f"{front}\n{back}", subject_name)
        if not md:
            raise HTTPException(502, "The model did not return an explanation. Try again.")
        db = SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            card.deep_explanation = md
            db.commit()
        finally:
            db.close()
        return {"explanation": md, "cached": False}

    # ------------------------------------------------------------------ overview + stats

    @router.get("/overview")
    def overview(request: Request):
        return overview_payload(_owner(request))

    @router.get("/stats")
    def stats(request: Request, days: int = 42):
        return stats_payload(_owner(request), days=days)

    @router.get("/materials/{material_id}/file")
    def material_file(request: Request, material_id: str):
        """Serve a material's source file inline so the in-pane viewer can frame
        it (see SecurityHeadersMiddleware). Deliberately narrow: the caller must
        own the material, and only PDFs and images are served — anything else
        would be an HTML/script payload rendered on our own origin."""
        import mimetypes as _mt

        from fastapi.responses import FileResponse

        user = _owner(request)
        db = SessionLocal()
        try:
            return _get_stats(db, user, days=days, now=_utcnow_naive())
        finally:
            db.close()
        if not file_id:
            raise HTTPException(404, "This material has no source file")
        path = _resolve_uploaded_file(file_id)
        mime = _mt.guess_type(path)[0] or "application/octet-stream"
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise HTTPException(415, "Only PDFs and images can be previewed")
        return FileResponse(
            path, media_type=mime, filename=name,
            headers={"X-Content-Type-Options": "nosniff"},
            content_disposition_type="inline",
        )

    @router.get("/calibration")
    def calibration(request: Request, days: int = 90):
        """Confidence calibration: Brier score, per-bucket stated vs actual
        accuracy, and the sure-but-wrong rate per subject."""
        return calibration_payload(_owner(request), days=days)

    @router.get("/calibration")
    def calibration(request: Request, days: int = 42):
        """Persistent, owner-scoped calibration curve (numeric confidence 0-100).

        Returns {curve: [...]} where each item has predicted, accuracy, total, low_n.
        """
        user = _owner(request)
        days = max(7, min(180, days))
        db = SessionLocal()
        try:
            since = _utcnow_naive() - timedelta(days=days)
            return {"curve": get_calibration_curve(db, user, since)}
        finally:
            db.close()

    @router.get("/history")
    def history(request: Request, limit: int = 100):
        return history_entries(_owner(request), limit=limit)

    @router.post("/reformat")
    async def reformat_text(request: Request, deck_id: Optional[str] = None):
        """One-time: reformat existing questions and cards to LaTeX (math) +
        Markdown, preserving content. Optionally scoped to one subject."""
        out = await run_reformat(_owner(request), deck_id=deck_id)
        return {"questions_reformatted": out["questions"],
                "cards_reformatted": out["cards"],
                "questions_scanned": out["questions_scanned"],
                "cards_scanned": out["cards_scanned"]}

    @router.post("/decks/{deck_id}/link-parts")
    async def link_parts(request: Request, deck_id: str):
        """Group multi-part problems (per material) and store each part's
        prerequisites — the earlier parts of the same problem. Practice then
        shows those earlier parts + your answers as exam-style context."""
        user = _owner(request)
        db = SessionLocal()
        try:
            study_service.get_deck(db, deck_id, user)
        finally:
            db.close()
        return await _link_deck_parts(user, deck_id)

    # The three maintenance passes below (and /reformat above) take an optional
    # deck_id so the subject view's "Tidy bank" panel only touches that subject.
    @router.post("/dedup")
    def dedup_questions_route(request: Request, deck_id: Optional[str] = None):
        return run_dedup(_owner(request), deck_id=deck_id)

    @router.post("/backfill-context")
    async def backfill_context(request: Request, deck_id: Optional[str] = None):
        return await run_backfill_context(_owner(request), deck_id=deck_id)

    @router.post("/audit-questions")
    async def audit_questions(request: Request, deck_id: Optional[str] = None):
        return await run_audit_questions(_owner(request), deck_id=deck_id)

    @router.get("/questions/{question_id}/prereqs")
    def question_prereqs(request: Request, question_id: str):
        """The previous part this question depends on, with its question,
        the user's latest answer to it, and the correct answer — for the
        'Earlier in this problem' context box during practice."""
        user = _owner(request)
        db = SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            ids = json.loads(row.prereq_ids) if row.prereq_ids else []
            out = []
            for pid in reversed(ids):
                pq = db.query(StudyQuestion).filter(StudyQuestion.id == pid).first()
                if not pq or (user is not None and pq.owner != user):
                    continue
                if _same_study_question(row, pq):
                    continue
                if _same_or_later_study_part(row, pq):
                    continue
                if pq.qtype == "mcq" and pq.options is not None and pq.correct_index is not None:
                    opts = json.loads(pq.options)
                    correct = opts[pq.correct_index] if 0 <= pq.correct_index < len(opts) else ""
                else:
                    correct = pq.reference or ""
                att = db.query(StudyAttempt).filter(StudyAttempt.question_id == pid)
                if user is not None:
                    att = att.filter(StudyAttempt.owner == user)
                att = att.order_by(StudyAttempt.attempted_at.desc()).first()
                out.append({
                    "id": pq.id, "number": pq.number, "question": pq.question,
                    "your_answer": att.answer if att else None,
                    "correct": correct,
                })
                break
            return {"prereqs": out}
        finally:
            db.close()

    @router.post("/optimize")
    def optimize_weights(request: Request):
        """Trigger per-user FSRS weight fitting (A6)."""
        user = _owner(request)
        db = SessionLocal()
        try:
            # ----- load this user's review history -----
            # Need cards owned by user for initial snapshots.
            cards = db.query(StudyCard).filter(StudyCard.owner == user).all() if user else []
            snapshots = []
            for c in cards:
                snapshots.append({
                    "id": c.id,
                    "state": c.state,
                    "stability": c.stability,
                    "difficulty": c.difficulty,
                    "last_review": _iso(c.last_review),
                    "reps": c.reps,
                    "lapses": c.lapses,
                })
            reviews_q = db.query(StudyReview).filter(StudyReview.owner == user)
            reviews = []
            for r in reviews_q.order_by(StudyReview.reviewed_at.asc()).all():
                reviews.append({
                    "id": r.id,
                    "card_id": r.card_id,
                    "rating": r.rating,
                    "interval_days": r.interval_days,
                    "state_before": r.state_before,
                    "reviewed_at": _iso(r.reviewed_at),
                })
            # Gate / fit
            fitted = fsrs_optimizer.fit_w(snapshots, reviews, seed=42)
            if fitted is None:
                return {"status": "insufficient_data",
                        "reviews": len(reviews),
                        "min_required": fsrs_optimizer.MIN_REVIEWS}
            # Persist
            row = db.query(StudyUserParams).filter(StudyUserParams.owner == user).first()
            now = _utcnow_naive()
            if row is None:
                db.add(StudyUserParams(
                    id=str(uuid.uuid4()), owner=user,
                    w_json=json.dumps(fitted),
                    review_count=len(reviews),
                    fitted_at=now,
                ))
            else:
                row.w_json = json.dumps(fitted)
                row.review_count = len(reviews)
                row.fitted_at = now
            db.commit()
            return {"status": "ok", "reviews": len(reviews), "w": fitted}
        finally:
            db.close()

    return router
