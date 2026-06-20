# routes/study_routes.py
"""Study module API — evidence-based studying built into Odysseus.

Endpoints:
  /api/study/overview              dashboard: due counts, streak, today's plan
  /api/study/decks ...             flashcard deck CRUD
  /api/study/decks/{id}/cards ...  card CRUD (single + bulk)
  /api/study/queue                 FSRS review queue (due + capped new)
  /api/study/cards/{id}/review     apply a rating (FSRS schedule + review log)
  /api/study/ai/generate-cards     LLM: source text -> proposed flashcards
  /api/study/ai/quiz               LLM/deck: free-recall quiz questions
  /api/study/ai/grade              LLM: grade a free-recall answer
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
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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
)
from src.study_ai import (
    AUTHOR_QUESTIONS_SYSTEM,
    DISCOVER_QUESTIONS_SYSTEM,
    EXPLAIN_FURTHER_SYSTEM,
    EXPLAIN_SYSTEM,
    EXTRACT_QUESTIONS_SYSTEM,
    FIGURE_CAPTION_SYSTEM,
    GRADE_OPEN_SYSTEM,
    HINT_SYSTEM,
    LOCATE_MATERIAL_SYSTEM,
    MATERIAL_CATEGORIES,
    REPAIR_JSON_SYSTEM,
    STUDY_NOTES_SYSTEM,
    SUBJECT_OVERVIEW_SYSTEM,
    chunk_material,
    classify_material,
    dedupe_questions,
    missing_question_numbers,
    normalize_questions,
    parse_answer_key_pages,
    parse_llm_json,
    parse_question_manifest,
    question_is_conclusion,
    question_key,
    rating_from_outcome,
)
from src.auth_helpers import get_current_user
from src import fsrs
from src.study_plan import generate_plan

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


class ExamUpdate(BaseModel):
    title: Optional[str] = None
    exam_date: Optional[str] = None
    exam_format: Optional[str] = None
    hours_per_week: Optional[float] = None
    rest_days: Optional[List[int]] = None
    topics: Optional[List[Dict[str, Any]]] = None
    archived: Optional[bool] = None


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
    answer: Optional[str] = None         # open
    confidence: Optional[str] = None     # "sure" | "unsure" | "guess"
    hints_used: int = 0
    duration_ms: Optional[int] = None


class HintIn(BaseModel):
    level: int = 1


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


def _material_to_dict(m: StudyMaterial) -> Dict:
    return {
        "id": m.id, "deck_id": m.deck_id, "name": m.name, "kind": m.kind,
        "file_id": m.file_id, "char_count": m.char_count or 0,
        "question_count": m.question_count or 0,
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


def _question_to_dict(q: StudyQuestion, with_answer: bool = True) -> Dict:
    out = {
        "id": q.id, "deck_id": q.deck_id, "material_id": q.material_id,
        "qtype": q.qtype, "question": q.question,
        "options": json.loads(q.options) if q.options else None,
        "topic": q.topic, "difficulty": q.difficulty,
        "origin": q.origin, "suspended": bool(q.suspended),
        "state": q.state or "new", "due": _iso(q.due),
        "reps": q.reps or 0, "lapses": q.lapses or 0,
    }
    if with_answer:
        out["correct_index"] = q.correct_index
        out["reference"] = q.reference
        out["explanation"] = q.explanation
    return out


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


async def _discover_questions_vision(owner, pdf_path: str) -> tuple:
    """Discovery pass: list every question label + the answer-key pages.

    ALL pages in ONE call, so the coverage check sees the whole document,
    not one batch at a time. Full render scale — low-res renders proved
    illegible to the vision model (degenerate repetition replies).
    Best-effort: returns ([], []) on any failure rather than blocking.
    (Study Bench's manifest approach.)

    Returns (manifest, answer_key_pages).
    """
    from src.study_vision import pages_to_data_urls, render_pdf_pages

    try:
        urls = pages_to_data_urls(render_pdf_pages(pdf_path))
        value = await _llm_json_vision(
            owner, DISCOVER_QUESTIONS_SYSTEM,
            f"List every explicit question in these {len(urls)} pages.",
            urls, max_tokens=8000, timeout=240)
        return parse_question_manifest(value), parse_answer_key_pages(value)
    except HTTPException as e:
        logger.warning("study discovery: vision manifest failed: %s", e.detail)
        return [], []
    except Exception as e:
        logger.warning("study discovery: vision manifest failed: %s", e)
        return [], []


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
    system = EXTRACT_QUESTIONS_SYSTEM if mode == "extract" else AUTHOR_QUESTIONS_SYSTEM
    type_note = ("Only produce questions of type: " + ", ".join(types) + ". ")         if len(types) == 1 else ""
    verb = ("Extract every practice question visible in these exam pages, "
            "transcribing all mathematics as LaTeX ($...$ inline, $$...$$ "
            "display) faithfully to the original."
            if mode == "extract" else
            "Write practice questions from the content visible in these pages.")

    manifest, answer_key_pages = await _discover_questions_vision(owner, pdf_path) \
        if mode == "extract" else ([], [])
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
            collected.extend(normalize_questions(value))

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
                raw_count += len(fresh)
                collected.extend(fresh)
            except HTTPException as e:
                if e.status_code == 503:
                    raise
                logger.warning("study coverage: targeted page %d failed: %s",
                               page_num, e.detail)
        coverage = _coverage_report(manifest, collected)

    return collected, raw_count, len(batches), errors, coverage


CARD_AUTHOR_SYSTEM = """You write flashcards for spaced repetition, following the minimum-information principle.

Rules:
- Each card tests exactly ONE atomic fact, distinction, or step. Split compound ideas into several cards.
- "front" is a specific retrieval cue phrased as a question (or a cloze-style prompt) — never a topic heading.
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

def setup_study_routes():
    router = APIRouter(prefix="/api/study", tags=["study"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

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
            deck = _get_deck(db, deck_id, user)
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
            deck = _get_deck(db, deck_id, user)
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
            deck = _get_deck(db, deck_id, user)
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
            deck = _get_deck(db, deck_id, user)
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
            card = _get_card(db, card_id, user)
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
                _get_deck(db, body.deck_id, user)  # ownership check
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
            card = _get_card(db, card_id, user)
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
            decks = ([_get_deck(db, deck_id, user)] if deck_id else
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
            # Stable global order: learning/relearning, review, new — already
            # per-deck; interleave decks by sorting on (phase, due).
            phase_rank = {"learning": 0, "relearning": 0, "review": 1, "new": 2}
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
            card = _get_card(db, card_id, user)
            deck = _get_deck(db, card.deck_id, user)
            state_before = card.state or "new"
            result = fsrs.schedule(
                _card_fsrs_dict(card), body.rating,
                desired_retention=_flt(deck.retention, 0.9),
            )
            card.state = result["state"]
            card.stability = str(result["stability"])
            card.difficulty = str(result["difficulty"])
            card.due = _to_naive_utc(result["due"])
            card.last_review = _to_naive_utc(result["last_review"])
            card.reps = result["reps"]
            card.lapses = result["lapses"]
            db.add(StudyReview(
                id=str(uuid.uuid4()), owner=user, card_id=card.id,
                deck_id=card.deck_id, rating=body.rating,
                state_before=state_before,
                interval_days=result["interval_days"],
                duration_ms=body.duration_ms,
                reviewed_at=_utcnow_naive(),
            ))
            db.commit()
            return {"card": _card_to_dict(card), "interval_days": result["interval_days"]}
        finally:
            db.close()

    # ------------------------------------------------------------------ AI

    @router.post("/ai/generate-cards")
    async def ai_generate_cards(request: Request, body: GenerateCardsIn):
        """Source text -> proposed cards. Returns proposals; nothing is saved."""
        user = _owner(request)
        text = (body.text or "").strip()
        if body.material_id:
            db = SessionLocal()
            try:
                m = db.query(StudyMaterial).filter(StudyMaterial.id == body.material_id).first()
                if not m or (user is not None and m.owner != user):
                    raise HTTPException(404, "Material not found")
                text = (m.content or "").strip()
            finally:
                db.close()
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
        user = _owner(request)
        count = max(1, min(20, body.count))
        if body.deck_id:
            db = SessionLocal()
            try:
                deck = _get_deck(db, body.deck_id, user)
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
            exam = _get_exam(db, exam_id, user)
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
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.delete("/exams/{exam_id}")
    def delete_exam(request: Request, exam_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            exam = _get_exam(db, exam_id, user)
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
            exam = _get_exam(db, exam_id, user)
            topics = json.loads(exam.topics) if exam.topics else []
            try:
                plan = generate_plan(
                    date.fromisoformat(exam.exam_date),
                    topics,
                    hours_per_week=_flt(exam.hours_per_week, 7.0),
                    rest_days=json.loads(exam.rest_days) if exam.rest_days else None,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            exam.plan = json.dumps(plan)
            exam.done_blocks = json.dumps([])  # plan changed; reset checkmarks
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.post("/exams/{exam_id}/toggle-block")
    def toggle_plan_block(request: Request, exam_id: str, body: ToggleBlockIn):
        user = _owner(request)
        db = SessionLocal()
        try:
            exam = _get_exam(db, exam_id, user)
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

    @router.get("/decks/{deck_id}/materials")
    def list_materials(request: Request, deck_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = _get_deck(db, deck_id, user)
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
            deck = _get_deck(db, deck_id, user)
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
            m = _get_material(db, material_id, user)
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
            m = _get_material(db, material_id, user)
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
            m = _get_material(db, material_id, user)
            if not m.file_id:
                raise HTTPException(400, "This material is pasted text, not a file.")
            file_id = m.file_id
        finally:
            db.close()
        text = _extract_file_text(file_id, user)  # max_chars=None -> full text
        db = SessionLocal()
        try:
            m = _get_material(db, material_id, user)
            before = m.char_count or 0
            m.content = text
            m.char_count = len(text)
            db.commit()
            return {"ok": True, "char_count": len(text), "previous": before}
        finally:
            db.close()

    # ------------------------------------------------------------ study notes

    @router.get("/figures/{material_id}/{idx}")
    def get_study_figure(request: Request, material_id: str, idx: int):
        """Serve one extracted figure image inline (embedded in study notes)."""
        import os
        from fastapi.responses import FileResponse
        user = _owner(request)
        db = SessionLocal()
        try:
            _get_material(db, material_id, user)  # ownership check
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
            m = _get_material(db, material_id, user)
            return {"summary": m.summary or "", "name": m.name, "file_id": m.file_id}
        finally:
            db.close()

    @router.post("/materials/{material_id}/notes")
    async def generate_material_notes(request: Request, material_id: str):
        """Generate (or regenerate) consultable study notes for one material:
        a Markdown summary from the full text, plus a Key-figures section with
        figures pulled from the source PDF and cited to their page."""
        user = _owner(request)
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

    @router.get("/decks/{deck_id}/overview")
    def get_deck_overview(request: Request, deck_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = _get_deck(db, deck_id, user)
            return {"overview": deck.overview or ""}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/overview")
    async def generate_deck_overview(request: Request, deck_id: str):
        """Generate a short subject overview from the chapter notes (preferred)
        or raw material text, tying the chapters together."""
        user = _owner(request)
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

    @router.post("/materials/{material_id}/extract")
    async def extract_questions(request: Request, material_id: str, body: ExtractIn):
        """AI question extraction: material text -> saved question bank items.

        mode "extract": pull the actual questions out of past papers/problem
        sets, faithfully. mode "author": write new exam-style questions from
        notes. Long materials are chunked; partial results are kept (JSON
        repair recovers complete objects from malformed replies).
        """
        user = _owner(request)
        mode = body.mode if body.mode in ("extract", "author") else "extract"
        types = [t for t in (body.types or ["mcq", "open"]) if t in ("mcq", "open")] or ["mcq", "open"]
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
                    options=json.dumps(q["options"]) if q["options"] else None,
                    correct_index=q["correct_index"], reference=q["reference"],
                    topic=q["topic"], difficulty=q["difficulty"],
                    origin="extracted" if mode == "extract" else "authored",
                    state="new", due=now,
                )
                db.add(row)
                saved.append(row)
            m = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
            if m:
                m.question_count = (m.question_count or 0) + len(saved)
            db.commit()
            return {
                "created": len(saved),
                "duplicates": duplicates,
                "chunks": len(chunks),
                "chunk_errors": errors,
                "vision": used_vision,
                "coverage": coverage,
                "questions": [_question_to_dict(r) for r in saved],
            }
        finally:
            db.close()

    # ------------------------------------------------------------------ question bank (v2)

    @router.get("/decks/{deck_id}/questions")
    def list_questions(request: Request, deck_id: str, q: Optional[str] = None,
                       qtype: Optional[str] = None):
        user = _owner(request)
        db = SessionLocal()
        try:
            deck = _get_deck(db, deck_id, user)
            query = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck.id)
            if user is not None:
                query = query.filter(StudyQuestion.owner == user)
            if qtype in ("mcq", "open"):
                query = query.filter(StudyQuestion.qtype == qtype)
            if q:
                query = query.filter(StudyQuestion.question.ilike(f"%{q}%"))
            rows = query.order_by(StudyQuestion.created_at.desc()).all()
            return {"questions": [_question_to_dict(r) for r in rows]}
        finally:
            db.close()

    @router.put("/questions/{question_id}")
    def update_question(request: Request, question_id: str, body: QuestionUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
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
            return _question_to_dict(row)
        finally:
            db.close()

    @router.delete("/questions/{question_id}")
    def delete_question(request: Request, question_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
            db.query(StudyAttempt).filter(StudyAttempt.question_id == row.id).delete()
            db.delete(row)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ------------------------------------------------------------------ practice (v2)

    @router.get("/practice/queue")
    def practice_queue(request: Request, deck_id: Optional[str] = None, limit: int = 20):
        """Due questions first (spaced retrieval), then new ones interleaved
        across topics (round-robin) instead of blocked by topic."""
        user = _owner(request)
        limit = max(1, min(100, limit))
        db = SessionLocal()
        try:
            now = _utcnow_naive()
            base = db.query(StudyQuestion).filter(
                StudyQuestion.suspended == False)  # noqa: E712
            if deck_id:
                _get_deck(db, deck_id, user)
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
            queue = due + interleaved
            return {"queue": [_question_to_dict(r, with_answer=False)
                              for r in queue[:limit]],
                    "due": len(due), "total": len(queue)}
        finally:
            db.close()

    @router.post("/questions/{question_id}/attempt")
    async def attempt_question(request: Request, question_id: str, body: AttemptIn):
        """Submit an answer. MCQ is checked locally; open answers are AI-graded.
        The outcome maps to an FSRS rating so practice is spaced automatically."""
        user = _owner(request)
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
            qtype = row.qtype
            options = json.loads(row.options) if row.options else []
            question_text = row.question
            reference = row.reference or ""
            correct_index = row.correct_index
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

        confidence = body.confidence if body.confidence in ("sure", "unsure", "guess") else None
        rating = rating_from_outcome(qtype, correct=correct, score=score,
                                     hints_used=body.hints_used or 0,
                                     confidence=confidence)

        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
            result = fsrs.schedule({
                "state": row.state or "new",
                "stability": _flt(row.stability),
                "difficulty": _flt(row.fsrs_difficulty),
                "last_review": row.last_review,
                "reps": row.reps or 0,
                "lapses": row.lapses or 0,
            }, rating)
            row.state = result["state"]
            row.stability = str(result["stability"])
            row.fsrs_difficulty = str(result["difficulty"])
            row.due = _to_naive_utc(result["due"])
            row.last_review = _to_naive_utc(result["last_review"])
            row.reps = result["reps"]
            row.lapses = result["lapses"]
            db.add(StudyAttempt(
                id=str(uuid.uuid4()), owner=user, question_id=row.id,
                deck_id=row.deck_id, qtype=qtype, answer=answer_text[:4000],
                correct=correct, score=score, rating=rating,
                confidence=confidence, hints_used=body.hints_used or 0,
                grading=json.dumps(grading) if grading else None,
                duration_ms=body.duration_ms, attempted_at=_utcnow_naive(),
            ))
            db.commit()
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
        user = _owner(request)
        level = max(1, min(3, body.level))
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
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

    @router.post("/questions/{question_id}/explain")
    async def question_explain(request: Request, question_id: str):
        """Post-attempt explanation for an MCQ (cached on the question)."""
        user = _owner(request)
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
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
            row = _get_question(db, question_id, user)
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
        user = _owner(request)
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
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
            row = _get_question(db, question_id, user)
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
        user = _owner(request)
        db = SessionLocal()
        try:
            row = _get_question(db, question_id, user)
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
        user = _owner(request)
        db = SessionLocal()
        try:
            card = _get_card(db, card_id, user)
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
            card = _get_card(db, card_id, user)
            card.deep_explanation = md
            db.commit()
        finally:
            db.close()
        return {"explanation": md, "cached": False}

    # ------------------------------------------------------------------ overview + stats

    @router.get("/overview")
    def overview(request: Request):
        user = _owner(request)
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
                              "days_left": days_left, "has_plan": bool(plan),
                              "today_blocks": today_blocks})

            att_q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= day_start)
            if user is not None:
                att_q = att_q.filter(StudyAttempt.owner == user)
            today_attempts = att_q.all()
            att_ok = sum(1 for t in today_attempts
                         if (t.correct is True) or ((t.score or 0) >= 60))

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

    @router.get("/stats")
    def stats(request: Request, days: int = 42):
        user = _owner(request)
        days = max(7, min(180, days))
        db = SessionLocal()
        try:
            now = _utcnow_naive()
            since = now - timedelta(days=days)
            rev_q = db.query(StudyReview).filter(StudyReview.reviewed_at >= since)
            foc_q = db.query(StudyFocusSession).filter(StudyFocusSession.started_at >= since)
            if user is not None:
                rev_q = rev_q.filter(StudyReview.owner == user)
                foc_q = foc_q.filter(StudyFocusSession.owner == user)
            by_day: Dict[str, Dict] = {}
            for i in range(days + 1):
                d = (since + timedelta(days=i)).date().isoformat()
                by_day[d] = {"date": d, "reviews": 0, "again": 0, "focus_min": 0}
            for r in rev_q.all():
                k = r.reviewed_at.date().isoformat() if r.reviewed_at else None
                if k in by_day:
                    by_day[k]["reviews"] += 1
                    if r.rating == 1:
                        by_day[k]["again"] += 1
            for s in foc_q.all():
                k = s.started_at.date().isoformat() if s.started_at else None
                if k in by_day:
                    by_day[k]["focus_min"] += s.actual_min or 0
            total_reviews = sum(v["reviews"] for v in by_day.values())
            total_again = sum(v["again"] for v in by_day.values())
            card_q = db.query(StudyCard)
            if user is not None:
                card_q = card_q.filter(StudyCard.owner == user)
            return {
                "daily": sorted(by_day.values(), key=lambda v: v["date"]),
                "totals": {
                    "reviews": total_reviews,
                    "success_rate": round(1 - total_again / total_reviews, 3)
                    if total_reviews else None,
                    "cards": card_q.count(),
                    "focus_min": sum(v["focus_min"] for v in by_day.values()),
                },
            }
        finally:
            db.close()

    @router.get("/history")
    def history(request: Request, limit: int = 100):
        """Answered-question + card-review log, newest first. Reads the existing
        append-only StudyAttempt / StudyReview tables joined to their text."""
        user = _owner(request)
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

    return router
