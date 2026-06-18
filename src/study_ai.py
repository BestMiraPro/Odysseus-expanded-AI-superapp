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


def parse_llm_json(raw: str):
    """Best-effort parse of an LLM reply into a JSON value.

    Order: exact parse → fence-stripped parse → trailing-comma repair →
    first-array object recovery → first-object decode. Raises ValueError when
    nothing structured can be salvaged.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty LLM reply")
    text = _strip_fences(raw)

    for candidate in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
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

        out.append({
            "qtype": qtype,
            "question": question,
            "options": norm_options,
            "correct_index": correct_index,
            "reference": reference,
            "topic": (str(item.get("topic") or "").strip() or None),
            "difficulty": difficulty,
            "number": number or None,
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


_CONCLUSION_RE = re.compile(
    r"^\s*(conclude that|we conclude|in conclusion|hence[, ]|therefore[, ]|thus[, ])",
    re.IGNORECASE)


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


def question_key(text: str) -> str:
    """Normalized full-text identity used for duplicate detection.

    Full text, not a prefix: multi-part exam questions legitimately share a
    long problem preamble, and a prefix key would collapse them into one.
    """
    return re.sub(r"\W+", "", (text or "").lower())


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

EXTRACT_QUESTIONS_SYSTEM = """You extract practice questions from course material (past exams, problem sets, worked examples, lecture notes containing exercises).

Rules:
- Extract questions FAITHFULLY: keep the original wording, numbers, and all answer options. Do not invent easier paraphrases.
- If the material includes the solution or answer key, use it for "correct_index"/"reference". If it does not, derive the correct answer yourself and write a complete reference solution.
- For multi-part questions (a, b, c…), emit each part as its own question with enough context to stand alone.
- "type": "mcq" when the material gives answer options; otherwise "open".
- "reference" for open questions must be a complete model answer (the steps + the result), concise enough to grade against.
- "topic": a short topic label (2-4 words). "difficulty": "easy"|"medium"|"hard" judged against a typical exam.
- "number": the question's visible label in the source ("3", "16a"); null if unnumbered.
- Skip pure definitions of administrative text (deadlines, grading policy, etc.).
- The QUESTION text must ask something the learner has to work out. NEVER emit a worked-solution step, a conclusion, or an instruction-to-verify as a question (e.g. "Conclude that (3,3) is the solution", "Verify that the gradient is …", "Show that the system is equivalent to (3,3,2,6,0)"). When the source is a solution walkthrough, recover the underlying QUESTION it answers and put the walkthrough in "reference"; never put the answer in the question text.
- Use the language of the source material.

Output ONLY a JSON array:
[{"number":"1","type":"mcq","question":"...","options":["...","..."],"correct_index":0,"reference":"...","topic":"...","difficulty":"medium"},
 {"number":"2a","type":"open","question":"...","reference":"...","topic":"...","difficulty":"hard"}]
No markdown, no commentary."""

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
- Use the language of the source material.

Output ONLY a JSON array in the same schema:
[{"type":"mcq","question":"...","options":["...","...","...","..."],"correct_index":2,"reference":"...","topic":"...","difficulty":"medium"},
 {"type":"open","question":"...","reference":"...","topic":"...","difficulty":"hard"}]
No markdown, no commentary."""

HINT_SYSTEM = """You give ONE progressive hint for a practice question. The student is mid-attempt: never reveal the final answer or full solution.

Hint levels:
1 = orientation: which concept/theorem/definition applies, nothing more.
2 = method: the first step or the structure of the approach, no numbers carried through.
3 = a worked start: begin the solution and stop roughly halfway, before the result becomes obvious.

Reply with the hint text only — plain text, 1-3 sentences (level 3 may use a short displayed step). No preamble like "Sure" or "Here's a hint", no reasoning narration, no answer."""

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
