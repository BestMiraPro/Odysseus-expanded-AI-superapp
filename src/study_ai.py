# src/study_ai.py
"""Pure helpers for the Study module's AI question pipeline.

No FastAPI, no DB, no network — everything here is unit-testable:
- robust JSON extraction/repair for LLM replies (fences, prose, trailing
  commas, truncated arrays — recovers every complete object it can)
- prompt constants for question extraction / authoring / hints / grading
- material chunking for long sources
- the outcome→FSRS-rating mapping that grounds practice in spaced retrieval

Rating mapping rationale (pinned by tests):
The FSRS rating is a report of retrieval quality, not a reward. Hints are
retrieval support, so a hinted success is "Hard" — the memory needed help.
"Easy" requires a clean, confident, unaided success; effortful success is
exactly the desirable difficulty we want more of, so it stays "Good".
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# JSON repair
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _recover_array_objects(text: str, start: int) -> List:
    """Walk a (possibly truncated/corrupt) JSON array, decoding object-by-object.

    Returns every complete top-level object found between `start` (the '[')
    and wherever the structure breaks. This is what lets a 90%-good LLM reply
    yield 90% of its questions instead of zero.
    """
    decoder = json.JSONDecoder()
    out: List = []
    i = start + 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        if text[i] != "{":
            # garbage between items — skip to the next plausible object
            nxt = text.find("{", i)
            if nxt == -1:
                break
            i = nxt
        try:
            obj, end = decoder.raw_decode(text[i:])
            out.append(obj)
            i += end
        except json.JSONDecodeError:
            # try minor repairs on the remaining slice once
            repaired = _TRAILING_COMMA_RE.sub(r"\1", text[i:])
            try:
                obj, end = decoder.raw_decode(repaired)
                out.append(obj)
                break  # offsets no longer line up after a repair pass
            except json.JSONDecodeError:
                break
    return out


_LATEX_BACKSLASH_RE = re.compile(r"\\([a-tA-Tv-zV-Z])")


def _escape_latex_backslashes(s: str) -> str:
    r"""Double backslashes that begin a LaTeX command (\ + letter, except \u
    which may be a JSON \uXXXX escape) so single-escaped LaTeX like "$\int$"
    parses as JSON. Best-effort: misses commands starting with u (rare)."""
    return _LATEX_BACKSLASH_RE.sub(r"\\\\\1", s)


def parse_llm_json(raw: str):
    """Best-effort parse of an LLM reply into a JSON value.

    Order: exact parse → fence-stripped parse → trailing-comma repair →
    first-array object recovery → first-object decode. Raises ValueError when
    nothing structured can be salvaged.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty LLM reply")
    text = _strip_fences(raw)

    # LaTeX in JSON: models often write "$\frac{a}{b}$" with single backslashes,
    # which is invalid JSON. Try a variant that escapes backslashes starting a
    # LaTeX command (\ + letter, except \u which may be a unicode escape) — only
    # as a fallback, after the clean parse, so valid JSON is never altered.
    comma_fixed = _TRAILING_COMMA_RE.sub(r"\1", text)
    for candidate in (text, comma_fixed,
                      _escape_latex_backslashes(text),
                      _escape_latex_backslashes(comma_fixed)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    arr_idx = text.find("[")
    obj_idx = text.find("{")

    # If an array opens first, decode it; on failure recover its members.
    # Recovery runs on the comma-repaired text so an object with a trailing
    # comma doesn't abort the scan for everything after it.
    if arr_idx != -1 and (obj_idx == -1 or arr_idx < obj_idx):
        try:
            value, _ = decoder.raw_decode(text[arr_idx:])
            return value
        except json.JSONDecodeError:
            fixed = _TRAILING_COMMA_RE.sub(r"\1", text)
            fixed_idx = fixed.find("[")
            recovered = _recover_array_objects(fixed, fixed_idx)
            if recovered:
                return recovered

    # Otherwise scan for the first decodable object/array anywhere.
    for start_char in ("{", "["):
        idx = text.find(start_char)
        while idx != -1:
            try:
                value, _ = decoder.raw_decode(text[idx:])
                return value
            except json.JSONDecodeError:
                idx = text.find(start_char, idx + 1)

    # Last resort: an array that opens but never closes properly.
    if arr_idx != -1:
        fixed = _TRAILING_COMMA_RE.sub(r"\1", text)
        recovered = _recover_array_objects(fixed, fixed.find("["))
        if recovered:
            return recovered
    raise ValueError("no JSON found in LLM reply")


# ---------------------------------------------------------------------------
# question normalization
# ---------------------------------------------------------------------------

VALID_QTYPES = ("mcq", "open")
VALID_DIFFICULTY = ("easy", "medium", "hard")


_LETTER_RE = re.compile(r"^\(?([a-hA-H])[\)\.:]?\s*(.*)$", re.DOTALL)


def _letter_to_index(ans: str, n_options: int) -> Optional[int]:
    """Map a letter answer ('b', '(c)', 'B.', 'd. The GDP...') to an option index."""
    m = _LETTER_RE.match(ans.strip())
    if not m:
        return None
    idx = ord(m.group(1).lower()) - ord("a")
    if not (0 <= idx < n_options):
        return None
    return idx


def _resolve_mcq_answer(item: Dict, options: List[str]) -> Optional[int]:
    """Find the correct option index from the many ways models report answers.

    Past papers and answer keys overwhelmingly use letters ('b', '(c)'), so
    letters are first-class here — this is the difference between extracting
    a whole exam and extracting nothing.
    """
    n = len(options)
    # 1) explicit index (int, or numeric/letter string)
    ci = item.get("correct_index", item.get("answer_index"))
    if isinstance(ci, str):
        ci_letter = _letter_to_index(ci, n)
        if ci_letter is not None:
            return ci_letter
    try:
        ci = int(ci)
        if 0 <= ci < n:
            return ci
    except (TypeError, ValueError):
        pass
    # 2) textual answer fields
    for key in ("answer", "correct", "correct_option", "correct_answer"):
        raw = item.get(key)
        if raw is None:
            continue
        ans = str(raw).strip()
        if not ans:
            continue
        low = ans.lower()
        # exact option text
        for i, o in enumerate(options):
            if o.lower() == low:
                return i
        # letter form (possibly followed by the option text)
        idx = _letter_to_index(ans, n)
        if idx is not None:
            return idx
        # prefix/containment match (needs enough length to avoid false hits)
        if len(low) >= 10:
            for i, o in enumerate(options):
                ol = o.lower()
                if ol.startswith(low[:40]) or low.startswith(ol[:40]):
                    return i
    return None


def context_is_redundant(question: str, context: str) -> bool:
    """True when a question's `context` adds nothing because the setup already
    sits verbatim inside the question text — showing it would just repeat the
    question. Compared on case-folded, alphanumeric-only text so formatting and
    whitespace differences don't matter."""
    nq = re.sub(r"[^a-z0-9]", "", (question or "").lower())
    nc = re.sub(r"[^a-z0-9]", "", (context or "").lower())
    return bool(nc) and nc in nq


def normalize_questions(value) -> List[Dict]:
    """Validate/clean raw LLM question objects into a uniform shape.

    Output items: {qtype, question, options(list|None), correct_index(int|None),
    reference, topic, difficulty}. Silently drops anything unusable.
    """
    if isinstance(value, dict):
        value = value.get("questions") or [value]
    if not isinstance(value, list):
        return []
    out: List[Dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("prompt") or "").strip()
        if not question:
            continue
        qtype = str(item.get("type") or item.get("qtype") or "").strip().lower()
        options = item.get("options") or item.get("choices")
        if qtype not in VALID_QTYPES:
            qtype = "mcq" if isinstance(options, list) and len(options) >= 2 else "open"

        correct_index: Optional[int] = None
        norm_options: Optional[List[str]] = None
        if qtype == "mcq":
            if not isinstance(options, list):
                continue
            norm_options = [str(o).strip() for o in options if str(o).strip()]
            if len(norm_options) < 2:
                continue
            correct_index = _resolve_mcq_answer(item, norm_options)
            if correct_index is None:
                continue  # an MCQ without a resolvable answer can't be checked

        reference = str(item.get("reference") or item.get("solution")
                        or item.get("answer") or "").strip()
        if qtype == "mcq" and not reference and norm_options is not None:
            reference = norm_options[correct_index]
        # Open questions without a reference are KEPT: past papers often ship
        # without solutions. The grader derives the correct answer at attempt
        # time when the reference is empty (see GRADE_OPEN_SYSTEM usage).
        if qtype == "open" and not reference:
            reference = ""

        difficulty = str(item.get("difficulty") or "medium").strip().lower()
        if difficulty not in VALID_DIFFICULTY:
            difficulty = "medium"

        raw_number = item.get("number")
        number = str(raw_number).strip() if raw_number is not None else ""
        try:
            source_page = max(1, int(item.get("source_page") or item.get("page")))
        except (TypeError, ValueError):
            source_page = None

        ctx = str(item.get("context") or item.get("setup") or "").strip() or None
        if ctx and context_is_redundant(question, ctx):
            ctx = None  # the setup is already in the question — don't repeat it

        out.append({
            "qtype": qtype,
            "question": question,
            "options": norm_options,
            "correct_index": correct_index,
            "reference": reference,
            "topic": (str(item.get("topic") or "").strip() or None),
            "difficulty": difficulty,
            "number": number or None,
            "source_page": source_page,
            "context": ctx,
        })
    return out


# ---------------------------------------------------------------------------
# discovery manifest / coverage
# ---------------------------------------------------------------------------

_QNUM_PREFIX_RE = re.compile(r"^(question|exercise|problem|ex|q|p)(?=\d)")


def canonical_qnum(label) -> str:
    """Canonical form of a question label for coverage comparison.

    "16 b)", "16b" and "Q16.B" compare equal; "Q4.", "04" and "4" too. Used
    to match discovery-manifest numbers against extracted-question numbers,
    where the model's labeling style can differ between the two passes.
    """
    s = re.sub(r"[^a-z0-9]", "", str(label or "").lower())
    s = _QNUM_PREFIX_RE.sub("", s)
    return re.sub(r"^0+(?=\d)", "", s)


def parse_question_manifest(value) -> List[Dict]:
    """Validate/clean a discovery-pass reply into [{number, kind, page}].

    Tolerates the {"questions": [...]} wrapper, int/str field mixups, and
    junk entries. Entries without a usable number are dropped; duplicates
    (by canonical number) keep the first occurrence.
    """
    if isinstance(value, dict):
        value = value.get("questions")
    if not isinstance(value, list):
        return []
    out: List[Dict] = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_number = item.get("number")
        number = str(raw_number).strip() if raw_number is not None else ""
        key = canonical_qnum(number)
        if not key or key in seen:
            continue
        seen.add(key)
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in VALID_QTYPES:
            kind = "open"
        try:
            page = max(1, int(item.get("page")))
        except (TypeError, ValueError):
            page = 1
        out.append({"number": number, "kind": kind, "page": page})
    return out


def missing_question_numbers(manifest: List[Dict], extracted_numbers) -> List[Dict]:
    """Manifest entries not covered by any extracted question number."""
    have = {canonical_qnum(n) for n in (extracted_numbers or [])}
    have.discard("")
    return [m for m in (manifest or [])
            if canonical_qnum(m.get("number")) not in have]


def offset_manifest(manifest: List[Dict], page_offset: int) -> List[Dict]:
    """Shift a batch-relative discovery manifest to document page numbers."""
    if not page_offset:
        return list(manifest or [])
    return [{**m, "page": int(m.get("page") or 1) + page_offset} for m in (manifest or [])]


def should_use_vision(*, has_pdf: bool, vision_available: bool,
                      explicit: Optional[bool] = None) -> bool:
    """Extraction path decision. ``explicit`` True/False forces a path (vision
    still needs the PDF); None means "vision by default when possible": the
    original PDF is on disk and a vision-capable model is configured."""
    if not has_pdf:
        return False
    if explicit is not None:
        return bool(explicit)
    return bool(vision_available)


def parse_answer_key_pages(value) -> List[int]:
    """Answer-key/solutions page numbers from a discovery reply.

    Tolerant of string page numbers and junk; returns sorted unique ints >= 1.
    """
    if not isinstance(value, dict):
        return []
    pages = value.get("answer_key_pages")
    if not isinstance(pages, list):
        return []
    out = set()
    for p in pages:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            out.add(n)
    return sorted(out)


# High-precision openers that mark a worked-solution step / conclusion rather
# than a question. Deliberately conservative: "prove/show/verify that X" is a
# legitimate exam task, so those are NOT here — only phrasings that state a
# result instead of asking for one. The AI audit (SOLUTION_AUDIT_SYSTEM) is the
# nuance layer for "show that <quantity> is <value>"-style answer leaks.
_CONCLUSION_RE = re.compile(
    r"^\s*("
    r"conclude(\s+that|,|\s+in\s+particular)|we\s+(can\s+)?conclude|in\s+conclusion|"
    r"hence[, ]|therefore[, ]|thus[, ]|so\s+we\s+conclude|"
    r"it\s+follows\s+that|we\s+(have\s+)?(thus\s+|therefore\s+)?(shown|deduce|deduced|obtain(ed)?)|"
    r"as\s+(we\s+have\s+)?shown|from\s+the\s+above|this\s+(shows|proves|confirms)\s+that"
    r")", re.IGNORECASE)


def question_is_conclusion(q: Dict) -> bool:
    """True for 'questions' that state a conclusion instead of asking.

    These are worked-solution steps leaked into the question bank ("Conclude
    that (3,3) is the solution…") — they hand the learner the answer, which
    defeats closed-book practice. Only open questions are checked; MCQ stems
    are answerable regardless of phrasing.
    """
    if q.get("qtype") != "open":
        return False
    return bool(_CONCLUSION_RE.match(q.get("question") or ""))


# Formatting-only LaTeX commands: they change how math looks, not what it says,
# so two extractions of the same question (one plain "P"/"ln", one LaTeX
# "\mathbb{P}"/"\ln") must key equal. We drop these commands, then drop every
# remaining backslash so "\ln"->"ln" matches the plain spelling.
_LATEX_FMT_RE = re.compile(
    r"\\(?:mathbb|mathbf|mathrm|mathcal|mathsf|mathit|boldsymbol|operatorname|"
    r"text|textbf|textit|left|right|displaystyle|big|bigg|Big|Bigg|quad|qquad)\b")


def question_key(text: str) -> str:
    """Normalized full-text identity used for duplicate detection.

    Full text, not a prefix: multi-part exam questions legitimately share a
    long problem preamble, and a prefix key would collapse them into one.

    Notation-insensitive: the same question extracted twice — once in plain text,
    once in LaTeX — must collapse to one key, so cross-run dedupe catches it.
    """
    s = (text or "").lower()
    s = _LATEX_FMT_RE.sub("", s)   # drop formatting-only commands (\mathbb, ...)
    s = s.replace("\\", "")        # \ln -> ln, \alpha -> alpha, etc.
    return re.sub(r"[^a-z0-9]", "", s)


# ---------------------------------------------------------------------------
# material category (theory vs exam/answer-key)
# ---------------------------------------------------------------------------

MATERIAL_CATEGORIES = ("theory", "exam")

# A "theory" marker (chapter/lecture/notes/...) wins even when the name also
# says "solutions", so "ch2.1_withsolutions.pdf" stays theory; a bare
# "...RegularExam_SolutionTopics.pdf" is an exam/answer key.
_THEORY_MARK_RE = re.compile(
    r"(chapter|lecture|\bnotes\b|\bslides\b|\bunit\b|\bweek\b|ch\d|cap\d|aula|tema)", re.I)
_ANSWER_KEY_RE = re.compile(
    r"(exam|solution|resit|answer[\s_-]?key|gabarito|\bmock\b|\bquiz\b|\btest\b|past[\s_-]*paper|marking)", re.I)


def is_answer_key_material(name: str) -> bool:
    """True for exam / answer-key / solutions files (questions, not theory).
    Chapter/lecture files are never answer keys, even if they bundle solutions."""
    n = name or ""
    if _THEORY_MARK_RE.search(n):
        return False
    return bool(_ANSWER_KEY_RE.search(n))


def classify_material(name: str) -> str:
    """Best-guess category from a filename: "exam" or "theory" (the default).
    Used to auto-tag a material on upload; the user can override it."""
    return "exam" if is_answer_key_material(name) else "theory"


def dedupe_questions(questions: List[Dict]) -> List[Dict]:
    """Drop near-duplicate questions (same `question_key`)."""
    seen = set()
    out = []
    for q in questions:
        key = question_key(q["question"])
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------

def chunk_material(text: str, chunk_chars: int = 12000, max_chunks: int = 4) -> List[str]:
    """Split source text on paragraph boundaries into LLM-sized chunks."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    chunks: List[str] = []
    paragraphs = re.split(r"\n\s*\n", text)
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 > chunk_chars and buf:
            chunks.append(buf.strip())
            buf = ""
            if len(chunks) >= max_chunks:
                break
        # a single paragraph longer than the chunk size gets hard-split
        while len(p) > chunk_chars:
            chunks.append(p[:chunk_chars])
            p = p[chunk_chars:]
            if len(chunks) >= max_chunks:
                return chunks[:max_chunks]
        buf += p + "\n\n"
    if buf.strip() and len(chunks) < max_chunks:
        chunks.append(buf.strip())
    return chunks[:max_chunks]


# ---------------------------------------------------------------------------
# outcome → FSRS rating
# ---------------------------------------------------------------------------

def rating_from_outcome(qtype: str, *, correct: Optional[bool] = None,
                        score: Optional[int] = None, hints_used: int = 0,
                        confidence: Optional[str] = None) -> int:
    """Map a practice outcome to an FSRS rating (1 Again … 4 Easy).

    - failure is Again regardless of anything else
    - success that needed hints is Hard (assisted retrieval)
    - clean success is Good
    - Easy only for clean, confident ('sure'), high-quality success
    """
    hinted = (hints_used or 0) > 0
    if qtype == "mcq":
        if not correct:
            return 1
        if hinted:
            return 2
        return 4 if confidence == "sure" else 3
    # open-ended
    s = score or 0
    if s < 60:
        return 1
    if s < 85 or hinted:
        return 2
    return 4 if (s >= 95 and confidence == "sure") else 3


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

# Faithful-formatting guidance. _MATH_JSON_NOTE for prompts whose reply is JSON
# (the math lives in string-field values, so backslashes must be doubled);
# _MATH_TEXT_NOTE for prompts that reply in plain Markdown.
_MATH_JSON_NOTE = (
    "\n- Faithful formatting: keep the source's structure and notation (bold, "
    "lists, sub/superscripts, fractions, vectors, matrices, tables) — don't "
    "flatten to plain text. Write ALL mathematics as LaTeX: $...$ inline, "
    "$$...$$ display. Because the field values are inside JSON strings, every "
    "backslash MUST be doubled — write \\\\frac, \\\\int, \\\\sqrt, \\\\alpha "
    "(not \\frac)."
)
_MATH_TEXT_NOTE = (
    "\n\nFormatting: reply in Markdown, preserving the source's structure "
    "(bold, lists, sub/superscripts, tables). Write all mathematics as LaTeX: "
    "$...$ inline, $$...$$ display."
)

EXTRACT_QUESTIONS_SYSTEM = """You extract practice questions from course material (past exams, problem sets, worked examples, lecture notes containing exercises).

Rules:
- Extract questions FAITHFULLY: keep the original wording, numbers, and all answer options. Do not invent easier paraphrases.
- If the material includes the solution or answer key, use it for "correct_index"/"reference". If it does not, derive the correct answer yourself and write a complete reference solution.
- For multi-part questions, emit each part as its own question. When several parts share a common setup (a problem statement, given data/values, a defined function or model, a figure), do NOT cram it into every question's wording — put that shared setup in a separate "context" field on each part. "context" holds everything needed to understand the part on its own (the statement, the given values, the meaning of the symbols it uses, a short description of any figure) but NEVER the solution or answer. Omit "context" (or use null) for questions that already stand alone.
- "type": "mcq" when the material gives answer options; otherwise "open".
- "reference" for open questions must be a complete model answer (the steps + the result), concise enough to grade against.
- "topic": a short topic label (2-4 words). "difficulty": "easy"|"medium"|"hard" judged against a typical exam.
- "number": the question's visible label in the source ("3", "16a"); null if unnumbered.
- Skip pure definitions of administrative text (deadlines, grading policy, etc.).
- The QUESTION text must ask something the learner has to work out, and must NOT state the answer. "Prove that f is concave", "Show that g is continuous", "Verify that the constraints are differentiable" are fine (they ask for the work). But NEVER emit a question that already gives away its own result: a conclusion ("Conclude that (3,3) is the solution", "Therefore the point is optimal"), or a verify/show/compute instruction that names the specific result ("Verify that the gradient is $(-4(x-6),-4(y-4))$", "Show that the Hessian is $-4I$, hence concave", "Solve the system to get (3,3,2,6,0)"). When the source is a solution walkthrough, recover the underlying QUESTION it answers and move the result + steps into "reference"; never leave the answer in the question text.
- Use the language of the source material.

Output ONLY a JSON array:
[{"number":"1","type":"mcq","question":"...","options":["...","..."],"correct_index":0,"reference":"...","topic":"...","difficulty":"medium"},
 {"number":"2a","type":"open","question":"...","context":"...","reference":"...","topic":"...","difficulty":"hard"}]
No markdown fences or commentary around the JSON (LaTeX inside the field values is expected)."""
EXTRACT_QUESTIONS_SYSTEM += _MATH_JSON_NOTE

DISCOVER_QUESTIONS_SYSTEM = """You locate explicit practice/exam questions in course material (pages of a PDF or plain text). This is a DISCOVERY pass only: do not solve, transcribe, merge, or invent questions.

Rules:
- List EVERY explicit question whose prompt appears in the material: numbered, lettered, or bulleted.
- Multi-part questions (a, b, c...): list each part separately, e.g. "16a", "16b".
- "kind" is "mcq" when answer options are shown, otherwise "open".
- "page" is the 1-based page/image number the question STARTS on (use 1 for plain text).
- Worked solutions, answer keys and "solution topics" sections are NOT questions - skip them.
- Separately, in "answer_key_pages", list the 1-based page numbers that are wholly or mostly an answer key / solutions / "solution topics" section (empty list if none). These pages provide answers, not questions.

Output ONLY a JSON object:
{"questions": [{"number": "1", "kind": "mcq", "page": 1}, {"number": "16a", "kind": "open", "page": 7}], "answer_key_pages": [9, 10]}
No markdown, no commentary."""

AUTHOR_QUESTIONS_SYSTEM = """You write NEW exam-style practice questions from course material (notes, textbook sections), targeting 70-85% expected success for a student who studied the material once — effortful but doable.

Rules:
- Questions demand production or application: compute, derive, explain why, compare, apply to a new scenario. Avoid pure recognition.
- Mix types: prefer "open" for procedures/derivations; "mcq" (4 options, plausible distractors built from common errors) for concepts and discriminations.
- Every "open" question gets a complete "reference" model answer; every "mcq" gets "correct_index" plus a "reference" stating why that option is right.
- "topic": short label. "difficulty": "easy"|"medium"|"hard".
- Cover the material broadly rather than clustering on one section.
- If a question relies on a scenario, data, or definitions you set up, put that setup in a separate "context" field (the question text stays the task; never put the answer in "context"). Omit it for self-contained questions.
- Use the language of the source material.

Output ONLY a JSON array in the same schema:
[{"type":"mcq","question":"...","options":["...","...","...","..."],"correct_index":2,"reference":"...","topic":"...","difficulty":"medium"},
 {"type":"open","question":"...","context":"...","reference":"...","topic":"...","difficulty":"hard"}]
No markdown fences or commentary around the JSON (LaTeX inside the field values is expected)."""
AUTHOR_QUESTIONS_SYSTEM += _MATH_JSON_NOTE

HINT_SYSTEM = """You give ONE progressive hint for a practice question. The student is mid-attempt: never reveal the final answer or full solution.

Hint levels:
1 = orientation: which concept/theorem/definition applies, nothing more.
2 = method: the first step or the structure of the approach, no numbers carried through.
3 = a worked start: begin the solution and stop roughly halfway, before the result becomes obvious.

Reply with the hint text only — plain text, 1-3 sentences (level 3 may use a short displayed step). No preamble like "Sure" or "Here's a hint", no reasoning narration, no answer."""

ASK_COACH_SYSTEM = """You are a Socratic study coach. The student is in the MIDDLE of answering a practice question — they have NOT submitted yet — and is asking you for help so they can write a better answer themselves.

Your job is to make them THINK and recall. You must NOT do the work for them.

Hard rules:
- NEVER reveal the answer, the correct option, the final result/value, or any step that hands the answer over. NEVER write their answer for them.
- If they ask "what's the answer" / "just tell me", warmly refuse and instead give the smallest nudge that moves them forward.
- What you MAY do: restate/clarify what the question is really asking; name the relevant concept, definition, theorem, or method; ask a guiding question; point to what they should recall; or react to their reasoning so far (confirm a good direction, gently flag a wrong turn — without giving the fix outright).
- Keep replies short (1-4 sentences), warm, and aimed at getting THEM to produce the answer.

You are given the question and a reference solution FOR YOUR EYES ONLY — use it to steer accurately, but never disclose it. Reply in the student's language."""

ASK_TUTOR_SYSTEM = """You are a study tutor. The student has ALREADY submitted their answer to this practice question and now wants to understand it. Help them fully.

- Answer their question directly. You MAY use the correct answer and the reference solution now.
- Explain WHY the correct answer is right, clear up the specific misconception their attempt shows, go deeper on the underlying concept, and connect it to related ideas when useful.
- Stay grounded in the provided question and reference; don't invent facts they don't support. Be concise but complete; a short worked step or example is welcome.
- Reply in the student's language."""

EXPLAIN_SYSTEM = """You explain a multiple-choice question after the student answered. Be brief and exacting.

Structure (plain text, no markdown headers):
1. Why the correct option is correct — the core reason in 1-3 sentences.
2. For each wrong option, one short clause on the specific error it represents.
3. End with the one-line takeaway worth remembering.

Use the language of the question."""

REPAIR_JSON_SYSTEM = """You repair malformed JSON produced by another model.

Rules:
- Output ONLY the repaired JSON value (array or object). No markdown fences, no preamble, no commentary.
- Preserve ALL data: question text, options, correct_index, reference, topic, difficulty, scores, feedback.
- Fix syntax only: missing or trailing commas, unescaped quotes, smart quotes, unterminated strings, unclosed brackets, prose wrapped around the JSON.
- If the input is prose containing JSON fragments, return the largest coherent JSON value those fragments support.
- If the very end is an incomplete object, drop that object and close the array properly."""

GRADE_OPEN_SYSTEM = """You grade a learner's free-recall answer against a reference answer. Grade meaning, not wording; be exacting but fair. No credit for vague gestures at the topic. Partial credit for correct method with arithmetic slips.

Output ONLY a JSON object:
{"score": 0-100, "verdict": "correct"|"partial"|"incorrect", "feedback": "1-3 sentences: exactly what was missing or wrong, then the key point to remember", "followup": "one short probing question targeting the weakest part"}
Use the learner's language for feedback/followup. No markdown, no commentary."""

EXPLAIN_FURTHER_SYSTEM = """You give a student the theory they need to understand a practice question or flashcard, grounded in THEIR course material — and point them to where to review it.

You receive the question/card (and its answer), the AI study notes, and the subject's materials. Each material is given under a "=== MATERIAL <id>: <name> ===" header; PDF text is annotated with "[Page N text]:" markers.

Write a focused theoretical explanation: the concept(s), definition(s), and reasoning the answer rests on — enough to actually understand it, not just restate the answer. Stay faithful to the provided materials; do not invent facts they don't support.

Then locate the theory ACROSS ALL the materials:
- The theory usually lives in a DIFFERENT file from where the question came. Practice exams, problem sets and answer keys contain QUESTIONS, not theory — do not cite them as the theory source. Cite the lecture/theory material(s) that actually explain the concept.
- The theory may span several materials; list every relevant location.
- For each location give: "material_id" (the id from its header), "page" (1-based page from the [Page N] markers, or null if unknown), and a short "label" (e.g. the chapter/topic name).
- "summary_section": the heading of the relevant section in the AI study notes (e.g. "Key concepts"); null if none applies.

Output ONLY a JSON object:
{"explanation": "<markdown explanation>", "summary_section": "<string|null>", "locations": [{"material_id": "<id>", "page": <int|null>, "label": "<short label>"}]}
"locations" may be empty if no material covers the theory. Use the language of the materials. No prose outside the JSON."""

LOCATE_MATERIAL_SYSTEM = """You locate, in a student's own course materials, where the content needed to answer a practice question is found — so they can go read it.

You receive the question and the subject's materials. Each material is under a "=== MATERIAL <id>: <name> ===" header; PDF text is annotated with "[Page N text]:" markers.

Find the material(s) and page(s) that present the theory/method needed to answer the question. Rules:
- Cite the lecture/theory material that EXPLAINS the content. Practice exams, problem sets and answer keys hold questions, not theory — never cite them.
- Cite only genuinely relevant locations; if several materials cover it, list each.
- "page": the 1-based page from the [Page N] markers, or null if unknown.
- "label": a short human label for the location (the chapter/topic name).
- If NO provided material actually covers the content, return an empty list.

Output ONLY a JSON object:
{"locations": [{"material_id": "<id>", "page": <int|null>, "label": "<short label>"}]}
No prose outside the JSON."""

STUDY_NOTES_SYSTEM = """You write a study-notes document from course material (a chapter, lecture, or paper), for a student to consult while practising problems. Be faithful to the material — summarize and organize it, never invent content.

Structure it as Markdown, scaled to the material (omit empty sections):
- A one-paragraph overview of what this material covers.
- "## Key concepts" — the core ideas, each defined precisely and briefly.
- "## Key formulas & results" — each with what its symbols mean and WHEN to use it. Write math in LaTeX delimited with $...$ (inline) or $$...$$ (display).
- "## Methods & worked patterns" — the standard procedures/derivations the material teaches, as short step lists a student can follow on a new problem.
- "## Common pitfalls" — mistakes the material warns about or that the topic invites.

Rules:
- Optimize for fast consultation: tight prose, lists over paragraphs, bold the term being defined.
- Keep it comprehensive enough to solve the material's problems from, but do not pad.
- Use the language of the source material.
- Output ONLY the Markdown document. No preamble, no code fences around the whole thing."""

SUBJECT_OVERVIEW_SYSTEM = """You write a short subject overview that ties together several chapters of study notes, as a map for a student.

Given per-chapter summaries, output Markdown:
- A 2-4 sentence overview of the subject and how the chapters connect.
- "## Chapters" — one bullet per chapter: its title and the one or two things it's responsible for.
- "## Threads" — 2-5 themes/techniques that recur across chapters, noting which chapters they appear in.

Keep it brief (a map, not a re-summary). Use the language of the source material. Output ONLY the Markdown."""

FIGURE_CAPTION_SYSTEM = """You are shown figures extracted from course material. For EACH image, in order, write a one-line caption of what it shows and decide whether it is a substantive learning figure (diagram, graph, chart, plotted curve, labeled illustration, table image) as opposed to decorative or page furniture (logos, header/footer art, photos of people, cover images, scanned plain text).

Output ONLY a JSON array, one object per image in the order given:
[{"idx": 0, "caption": "...", "keep": true}, {"idx": 1, "caption": "...", "keep": false}]
Set "keep": false for anything decorative or that is just text. Use the language of the material for captions."""

# Faithful-formatting guidance appended to the relevant prompts. Plain-Markdown
# replies get the text note; JSON replies whose fields hold math get the
# JSON-escaping note.
LINK_PARTS_SYSTEM = """You receive the questions extracted from ONE exam/material, as a JSON list of {id, number, question}. Many are parts of the SAME multi-part problem: they share a setup (the same scenario, the same defined function/data, the same "Problem P") or are sequential parts (a, b, c…).

Group them into problems and order each group's parts as they appear in the exam (earliest part first).

- Put every question belonging to the same problem into one ordered group. Use the part labels/numbers and the shared setup/scenario to decide membership and order.
- A question that stands on its own is its own single-item group.
- Order matters: later parts rely on earlier parts' results, so the sequence must be correct.
- NEVER mix questions from different problems into one group.

Output ONLY JSON: {"groups": [["<id of part a>", "<id of part b>", "<id of part c>"], ["<id of a standalone>"], ...]} using the exact ids given. No commentary."""


def _num_order_key(num):
    """Sort key for a part label: '16a'->(16,'a'), '16'->(16,''), 'Q2.'->(2,'').
    Returns None when there's no usable numeric label."""
    if not num:
        return None
    s = re.sub(r"[^0-9a-z]", "", str(num).strip().lower())
    m = re.match(r"(\d+)([a-z]*)", s)
    if not m:
        return None
    return (int(m.group(1)), m.group(2))


def prereqs_from_groups(groups, valid_ids=None, number_by_id=None) -> Dict[str, List[str]]:
    """From ordered groups of question ids (LINK_PARTS_SYSTEM output), return
    {id: [earlier-part ids]} — the prerequisites a later part carries forward.

    Ordering:
    - A part WITH a numeric label ("16b") takes only same-group parts whose label
      is strictly smaller ("16a"). It deliberately ignores unnumbered siblings:
      their position relative to a labelled part can't be trusted, and a labelled
      exam part must NEVER inherit a later part (the bug this fixes).
    - A part WITHOUT a label takes the parts before it in the group's given
      (model) order.
    Unknown ids (not in `valid_ids`) are skipped; a part never includes itself."""
    number_by_id = number_by_id or {}
    out: Dict[str, List[str]] = {}
    for group in (groups or []):
        if not isinstance(group, (list, tuple)):
            continue
        ids = [str(q) for q in group
               if valid_ids is None or str(q) in valid_ids]
        keys = {qid: _num_order_key(number_by_id.get(qid)) for qid in ids}
        for idx, qid in enumerate(ids):
            k = keys[qid]
            if k is not None:
                out[qid] = [o for o in ids
                            if o != qid and keys[o] is not None and keys[o] < k]
            else:
                out[qid] = [o for o in ids[:idx] if o != qid]
    return out

SOLUTION_AUDIT_SYSTEM = """You audit a practice-question bank and flag entries that are NOT real questions: worked-solution steps, conclusions, or instructions that already STATE the result they pretend to ask for. Those leak the answer, so they are useless for closed-book practice.

You receive a JSON list of questions (each with an "id" and its text). For EACH, decide whether it is a genuine QUESTION the learner must work out, or a SOLUTION STATEMENT that hands over its own answer.

KEEP (genuine questions) — even when phrased as prove/show/verify/check:
- "Prove that f is concave", "Show that g is continuous", "Verify that the constraints are differentiable", "Find/Compute/Solve/Determine…". These ask the learner to produce the work and do NOT state the specific result.

FLAG (solution statements):
- It states the specific result/value/expression it asks to reach, e.g. "Verify that the gradient is $(-4(x-6),-4(y-4))$", "Show that the Hessian is $-4I$, hence f is concave", "Solve the system to get $(3,3)$".
- It is a pure conclusion: "Conclude that (3,3) is the solution", "Therefore the point is optimal", "Conclude that the Sufficiency Theorem applies to P".
- It is a narrated solution step rather than a prompt ("We compute the gradient and obtain …").

When unsure, KEEP it — only flag entries that clearly give away their own answer.

Output ONLY JSON: {"flag": ["<id>", ...]} — the ids of the solution statements. Empty list if all are genuine. No commentary."""

ADD_CONTEXT_SYSTEM = """You restore the shared SETUP that multi-part exam questions lost when they were split into separate questions.

You receive ONE material's text (a past exam / problem set; PDF text is annotated with "[Page N text]:" markers) and a list of questions extracted from it (each with an "id" and its text). Many questions are a single part of a larger problem and silently depend on a setup that the source states once for the whole problem — a problem statement, given data/values, a defined function or model, a figure, or an earlier part's result (signalled by phrases like "the objective function", "the function above", "the system", "using the previous result", "the same data").

For EACH question that cannot be fully understood on its own, write a concise "context": the shared setup it needs, recovered FAITHFULLY from the material — the problem statement, the given values, the meaning of the symbols it mentions, a short description of any figure.

Rules:
- Include ONLY the setup needed to understand and attempt the question. NEVER include the solution, the final answer, or steps toward it.
- Do not repeat the question itself inside "context".
- If a question already stands alone, OMIT it from the output — do not invent context.
- Use the material's language.

Output ONLY JSON: {"items": [{"id": "<id>", "context": "<setup>"}, ...]} — include only the questions that need context. No commentary."""

TRANSCRIBE_SYSTEM = """You transcribe pages of course material (scanned or formula-heavy PDFs) into faithful Markdown so a student can study from the text.

Rules:
- Transcribe EVERYTHING legible, in reading order, page by page. Begin each page with a line "[Page N text]:" (N = the document page number given in the instruction).
- Keep the original wording and language; do not summarize, solve, or comment.
- Write all mathematics as LaTeX ($...$ inline, $$...$$ display). Keep structure: headings, lists, tables (as Markdown tables), bold terms.
- Describe figures briefly in brackets, e.g. [Figure: supply and demand curves crossing at P*].
- If a page is blank or unreadable, write "[Page N text]:" followed by "(unreadable)".

Output ONLY the Markdown transcription. No preamble, no code fences around the whole thing."""

REFORMAT_SYSTEM = """You reformat already-extracted study text so it displays well. Convert all mathematics to LaTeX ($...$ inline, $$...$$ display) and fix Markdown formatting (sub/superscripts, fractions, lists, bold).

Rules:
- PRESERVE the content exactly: same wording, numbers, options (same count and order), and answers. Do NOT solve, rephrase, shorten, translate, or change meaning. You are only changing notation/formatting.
- You receive a JSON array of items. Return a JSON array with the SAME items — same "id" and same keys — with the text fields reformatted.
- Inside JSON strings double every backslash (\\\\frac, not \\frac).

Output ONLY the JSON array. No markdown fences or commentary around it."""

HINT_SYSTEM += _MATH_TEXT_NOTE
ASK_COACH_SYSTEM += _MATH_TEXT_NOTE
ASK_TUTOR_SYSTEM += _MATH_TEXT_NOTE
EXPLAIN_SYSTEM += _MATH_TEXT_NOTE
STUDY_NOTES_SYSTEM += _MATH_TEXT_NOTE
SUBJECT_OVERVIEW_SYSTEM += _MATH_TEXT_NOTE
GRADE_OPEN_SYSTEM += _MATH_JSON_NOTE
EXPLAIN_FURTHER_SYSTEM += _MATH_JSON_NOTE
