# Study Chapters & Themes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a practice session be scoped to one chapter of a document, or to one subject-wide theme, instead of only "everything in this subject".

**Architecture:** Two nullable grouping columns on `study_questions` (`chapter`/`chapter_index`, and `theme`) plus `chapter_count` on `study_materials`. Chapters come from the document's own headings (per material); themes come from clustering the `topic` labels already stored (per subject). Both are ordinary scope filters on the existing `practice_queue_payload`, so FSRS ordering, interleaving and mock mode keep working untouched. A single `GET /decks/{id}/groupings` call feeds the picker.

**Tech Stack:** FastAPI + SQLAlchemy (SQLite), vanilla-JS ES modules, pytest.

**Spec:** `website/superpowers/specs/2026-09-04-study-chapters-themes-design.md`

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `core/database.py` | ORM columns + idempotent SQLite migration | Modify |
| `routes/study/practice.py` | `chapter`/`theme` scoping in `practice_queue_payload` + route params | Modify |
| `routes/study/insights.py` | `groupings_payload` + `GET /decks/{id}/groupings` | Modify |
| `routes/study/maintenance.py` | `run_detect_chapters`, `run_cluster_themes` + routes | Modify |
| `routes/study_routes.py` | Re-export the two new services for the agent | Modify |
| `src/study_agent.py` | `maintain_bank` gains `detect_chapters` / `group_themes` | Modify |
| `static/js/study.js` | Per-subject practice picker + two Tidy-bank actions | Modify |
| `tests/test_study_chapters_themes.py` | All service-level tests for this feature | Create |

---

## Task 1: Schema — chapter, chapter_index, theme, chapter_count

**Files:**
- Modify: `core/database.py` (StudyQuestion ~line 2126, StudyMaterial ~line 2101, migration ~line 941)
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing test**

```python
"""Chapter and theme grouping for the Study question bank."""
import os

import pytest

from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture
def db(monkeypatch):
    import core.database as cd
    from routes.study import _common as common

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(common, "SessionLocal", SessionLocal)
    yield SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def test_grouping_columns_exist(db):
    """Chapters and themes need somewhere to live; all four are nullable so
    existing rows keep their meaning."""
    from core.database import StudyMaterial, StudyQuestion

    for col in ("chapter", "chapter_index", "theme"):
        assert hasattr(StudyQuestion, col), f"StudyQuestion.{col} missing"
    assert hasattr(StudyMaterial, "chapter_count")

    s = db()
    q = StudyQuestion(id="q1", deck_id="d1", question="x", chapter="1 — Intro",
                      chapter_index=1, theme="Integration")
    s.add(q)
    s.commit()
    got = s.query(StudyQuestion).filter_by(id="q1").first()
    assert (got.chapter, got.chapter_index, got.theme) == ("1 — Intro", 1, "Integration")
    s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py::test_grouping_columns_exist -q -p no:cacheprovider -W ignore`
Expected: FAIL with `AssertionError: StudyQuestion.chapter missing`

- [ ] **Step 3: Add the columns**

In `core/database.py`, in `class StudyQuestion`, directly after the `source_page` line:

```python
    # Where the question sits in its source document. Written by chapter
    # detection; null means the document was never split (see chapter_count).
    chapter         = Column(String, nullable=True, index=True)
    chapter_index   = Column(Integer, nullable=True)   # ordinal, so chapters sort
    # Coarse subject-wide grouping, clustered from `topic`. Independent of chapter.
    theme           = Column(String, nullable=True, index=True)
```

In `class StudyMaterial`, directly after the `page_count` line:

```python
    # 0/1 = not split (single chapter, or detection not run); >= 2 = offer chapters.
    chapter_count  = Column(Integer, nullable=True)
```

- [ ] **Step 4: Add the migration**

In `core/database.py`, immediately after the `source_page` migration (~line 941):

```python
        if "chapter" not in q_cols:
            conn.execute("ALTER TABLE study_questions ADD COLUMN chapter TEXT")
        if "chapter_index" not in q_cols:
            conn.execute("ALTER TABLE study_questions ADD COLUMN chapter_index INTEGER")
        if "theme" not in q_cols:
            conn.execute("ALTER TABLE study_questions ADD COLUMN theme TEXT")
        mat_cols2 = [r[1] for r in conn.execute("PRAGMA table_info(study_materials)")]
        if mat_cols2 and "chapter_count" not in mat_cols2:
            conn.execute("ALTER TABLE study_materials ADD COLUMN chapter_count INTEGER")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
git add core/database.py tests/test_study_chapters_themes.py
git commit -m "feat(study): chapter and theme columns for question grouping"
```

---

## Task 2: Scope the practice queue by chapter and theme

**Files:**
- Modify: `routes/study/practice.py` (`practice_queue_payload`, `practice_queue` route)
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_study_chapters_themes.py`:

```python
def _seed_chaptered(SessionLocal, owner="alice"):
    """One subject, one document, three chapters; plus a themed second doc."""
    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study._common import _utcnow_naive

    s = SessionLocal()
    s.add(StudyDeck(id="d1", owner=owner, name="Stats", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="m1", owner=owner, deck_id="d1", name="Workbook.pdf",
                        kind="pdf", content="x", char_count=1, chapter_count=3))
    s.add(StudyMaterial(id="m2", owner=owner, deck_id="d1", name="Exam.pdf",
                        kind="pdf", content="x", char_count=1, chapter_count=1))
    now = _utcnow_naive()
    for i in range(3):
        s.add(StudyQuestion(id=f"c1{i}", owner=owner, deck_id="d1", material_id="m1",
                            qtype="open", question=f"ch1 q{i}", reference="r",
                            topic="Bernoulli", chapter="1 — Probability", chapter_index=1,
                            theme="Probability", state="new", due=now))
    for i in range(2):
        s.add(StudyQuestion(id=f"c2{i}", owner=owner, deck_id="d1", material_id="m1",
                            qtype="open", question=f"ch2 q{i}", reference="r",
                            topic="Covariance", chapter="2 — Random variables",
                            chapter_index=2, theme="Distributions", state="new", due=now))
    # different document, same theme as chapter 2's questions
    s.add(StudyQuestion(id="e0", owner=owner, deck_id="d1", material_id="m2",
                        qtype="open", question="exam q", reference="r",
                        topic="Covariance", theme="Distributions", state="new", due=now))
    s.commit()
    s.close()


def test_queue_scoped_to_one_chapter(db):
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    out = practice_queue_payload("alice", deck_id="d1", chapter="1 — Probability", limit=20)

    assert {q["id"] for q in out["queue"]} == {"c10", "c11", "c12"}
    assert out["chapter"] == "1 — Probability"


def test_theme_spans_documents(db):
    """A theme is subject-wide: it must pull from every document that has it."""
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    out = practice_queue_payload("alice", deck_id="d1", theme="Distributions", limit=20)

    assert {q["id"] for q in out["queue"]} == {"c20", "c21", "e0"}
    assert len({q["material_id"] for q in out["queue"]}) == 2


def test_chapter_scope_keeps_spaced_order(db):
    """A chapter is a scope filter, not a replacement for scheduling: due
    questions still come before unseen ones."""
    from datetime import timedelta

    from core.database import StudyQuestion
    from routes.study._common import _utcnow_naive
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    s = db()
    s.add(StudyQuestion(id="due1", owner="alice", deck_id="d1", material_id="m1",
                        qtype="open", question="overdue", reference="r",
                        chapter="1 — Probability", chapter_index=1,
                        state="review", due=_utcnow_naive() - timedelta(days=5)))
    s.commit()
    s.close()

    out = practice_queue_payload("alice", deck_id="d1", chapter="1 — Probability",
                                 limit=20)
    assert out["queue"][0]["id"] == "due1"


def test_unknown_chapter_returns_empty_not_everything(db):
    """A chapter filter is exact — unlike the fuzzy topic filter it must not
    silently fall back to the whole subject."""
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    out = practice_queue_payload("alice", deck_id="d1", chapter="99 — Nope", limit=20)
    assert out["queue"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore -k "chapter or theme"`
Expected: FAIL with `TypeError: practice_queue_payload() got an unexpected keyword argument 'chapter'`

- [ ] **Step 3: Add the parameters**

In `routes/study/practice.py`, change the signature of `practice_queue_payload`:

```python
def practice_queue_payload(user, *, deck_id=None, material_id=None, topics=None,
                           limit: int = 20, mock: bool = False,
                           mode=None, adaptive: bool = False,
                           chapter=None, theme=None) -> Dict:
```

Add to its docstring, after the topic-filter paragraph:

```
    ``chapter`` scopes to one chapter of one document; ``theme`` scopes to one
    subject-wide theme, which may span several documents. Both are exact
    matches — unlike the fuzzy topic filter they never fall back to the whole
    subject, because an empty chapter is a real answer.
```

Immediately after the `if user is not None: base = base.filter(...)` line, add:

```python
        if chapter:
            base = base.filter(StudyQuestion.chapter == chapter)
        if theme:
            base = base.filter(StudyQuestion.theme == theme)
```

In the returned dict, beside `"mock": bool(mock),` add:

```python
            "chapter": chapter,
            "theme": theme,
```

- [ ] **Step 4: Pass them through the route**

In the same file, extend the `practice_queue` route signature and call:

```python
    @router.get("/practice/queue")
    def practice_queue(request: Request, deck_id: Optional[str] = None,
                       material_id: Optional[str] = None,
                       topics: Optional[str] = None,
                       limit: int = 20, mock: bool = False,
                       mode: Optional[str] = None, adaptive: bool = False,
                       chapter: Optional[str] = None,
                       theme: Optional[str] = None):
        """Due questions first (spaced retrieval), then new ones interleaved
        across subject and topic. Optional scope: one subject, one material, a
        comma-separated topic list, one document chapter, or one subject-wide
        theme. ``mock=true`` draws a fixed-size paper regardless of the
        schedule; ``mode=pretest`` lifts one unseen question per topic."""
        return practice_queue_payload(
            _owner(request), deck_id=deck_id, material_id=material_id,
            topics=topics, limit=limit, mock=mock, mode=mode, adaptive=adaptive,
            chapter=chapter, theme=theme)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (5 passed)

- [ ] **Step 6: Check nothing else regressed**

Run: `PYTHONUTF8=1 python -m pytest tests/ -q -p no:cacheprovider -W ignore -k "study" --continue-on-collection-errors`
Expected: 316+ passed, 0 failed

- [ ] **Step 7: Commit**

```bash
git add routes/study/practice.py tests/test_study_chapters_themes.py
git commit -m "feat(study): scope the practice queue by chapter or theme"
```

---

## Task 3: The groupings endpoint

**Files:**
- Modify: `routes/study/insights.py` (add `groupings_payload` before `register`, route inside `register`)
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_groupings_lists_chapters_and_themes(db):
    from routes.study.insights import groupings_payload

    _seed_chaptered(db)
    out = groupings_payload("alice", "d1")

    assert len(out["chapters"]) == 1                      # only the 3-chapter doc
    doc = out["chapters"][0]
    assert doc["material"] == "Workbook.pdf"
    assert [c["label"] for c in doc["chapters"]] == ["1 — Probability", "2 — Random variables"]
    assert [c["count"] for c in doc["chapters"]] == [3, 2]

    themes = {t["name"]: t for t in out["themes"]}
    assert themes["Distributions"]["count"] == 3
    assert themes["Distributions"]["materials"] == 2


def test_groupings_omits_single_chapter_documents(db):
    """Exam.pdf has chapter_count=1, so it must not appear as a chapter row."""
    from routes.study.insights import groupings_payload

    _seed_chaptered(db)
    names = [d["material"] for d in groupings_payload("alice", "d1")["chapters"]]
    assert "Exam.pdf" not in names


def test_groupings_empty_when_nothing_grouped(db):
    """A subject of plain exam papers looks exactly as it does today."""
    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study.insights import groupings_payload

    s = db()
    s.add(StudyDeck(id="d9", owner="alice", name="Plain", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="m9", owner="alice", deck_id="d9", name="Paper.pdf",
                        kind="pdf", content="x", char_count=1))
    s.add(StudyQuestion(id="p1", owner="alice", deck_id="d9", material_id="m9",
                        qtype="open", question="q", reference="r", state="new"))
    s.commit()
    s.close()

    out = groupings_payload("alice", "d9")
    assert out == {"chapters": [], "themes": []}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore -k groupings`
Expected: FAIL with `ImportError: cannot import name 'groupings_payload'`

- [ ] **Step 3: Implement `groupings_payload`**

In `routes/study/insights.py`, immediately before `def register(router: APIRouter) -> None:`:

```python
def groupings_payload(user, deck_id: str) -> Dict:
    """The two ways to slice a subject's bank, with counts, for the practice
    picker. Chapters belong to one document and only appear when that document
    actually has several (``chapter_count >= 2``); themes span the subject.
    Either list comes back empty when nothing has been grouped, which is the
    signal to hide that whole section."""
    db = _common.SessionLocal()
    try:
        study_service.get_deck(db, deck_id, user)

        mq = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
        qq = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck_id,
                                            StudyQuestion.suspended == False)  # noqa: E712
        if user is not None:
            mq = mq.filter(StudyMaterial.owner == user)
            qq = qq.filter(StudyQuestion.owner == user)

        multi = {m.id: m.name for m in mq.all() if (m.chapter_count or 0) >= 2}
        rows = qq.all()

        chapters = []
        for mid, mname in multi.items():
            buckets = {}
            for r in rows:
                if r.material_id != mid or not r.chapter:
                    continue
                b = buckets.setdefault(r.chapter, {"label": r.chapter,
                                                   "index": r.chapter_index or 0,
                                                   "count": 0})
                b["count"] += 1
            if buckets:
                chapters.append({
                    "material_id": mid,
                    "material": mname,
                    "chapters": sorted(buckets.values(),
                                       key=lambda c: (c["index"], c["label"])),
                })
        chapters.sort(key=lambda d: d["material"])

        themes = {}
        for r in rows:
            if not r.theme:
                continue
            t = themes.setdefault(r.theme, {"name": r.theme, "count": 0, "_mats": set()})
            t["count"] += 1
            if r.material_id:
                t["_mats"].add(r.material_id)
        out_themes = [{"name": t["name"], "count": t["count"],
                       "materials": len(t["_mats"])}
                      for t in themes.values()]
        out_themes.sort(key=lambda t: (-t["count"], t["name"]))

        return {"chapters": chapters, "themes": out_themes}
    finally:
        db.close()
```

- [ ] **Step 4: Add the route**

Inside `register`, next to the other read routes:

```python
    @router.get("/decks/{deck_id}/groupings")
    def deck_groupings(request: Request, deck_id: str):
        """Chapters (per document, only when it has several) and themes (subject
        wide) with counts — everything the practice picker needs in one call."""
        return groupings_payload(_owner(request), deck_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (8 passed)

- [ ] **Step 6: Commit**

```bash
git add routes/study/insights.py tests/test_study_chapters_themes.py
git commit -m "feat(study): groupings endpoint for the practice picker"
```

---

## Task 4: Detect chapters for an existing document

**Files:**
- Modify: `routes/study/maintenance.py`
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_detect_chapters_assigns_and_counts(db, monkeypatch):
    import asyncio

    from routes.study import maintenance as mnt

    _seed_chaptered(db)
    # wipe the seeded grouping so detection has real work to do
    s = db()
    for q in s.query(__import__("core.database", fromlist=["x"]).StudyQuestion).all():
        q.chapter, q.chapter_index = None, None
    s.query(__import__("core.database", fromlist=["x"]).StudyMaterial).filter_by(
        id="m1").first().chapter_count = None
    s.commit()
    s.close()

    async def fake_llm(owner, system, user, **kw):
        return {"chapters": [
            {"index": 1, "label": "1 — Probability", "questions": ["c10", "c11", "c12"]},
            {"index": 2, "label": "2 — Random variables", "questions": ["c20", "c21"]},
        ]}

    monkeypatch.setattr(mnt, "_llm_json", fake_llm)
    out = asyncio.run(mnt.run_detect_chapters("alice", "m1"))

    assert out["chapters"] == 2
    assert out["assigned"] == 5
    from core.database import StudyMaterial, StudyQuestion
    s = db()
    assert s.query(StudyMaterial).filter_by(id="m1").first().chapter_count == 2
    assert s.query(StudyQuestion).filter_by(id="c10").first().chapter == "1 — Probability"
    assert s.query(StudyQuestion).filter_by(id="c21").first().chapter_index == 2
    s.close()


def test_detect_chapters_refuses_to_split_one_chapter(db, monkeypatch):
    """The rule the whole feature turns on: fewer than two headings means the
    document is never split."""
    import asyncio

    from core.database import StudyMaterial, StudyQuestion
    from routes.study import maintenance as mnt

    _seed_chaptered(db)

    async def one_chapter(owner, system, user, **kw):
        return {"chapters": [{"index": 1, "label": "The whole paper",
                              "questions": ["c10", "c11"]}]}

    monkeypatch.setattr(mnt, "_llm_json", one_chapter)
    out = asyncio.run(mnt.run_detect_chapters("alice", "m1"))

    assert out["chapters"] == 1
    assert out["assigned"] == 0
    s = db()
    assert s.query(StudyMaterial).filter_by(id="m1").first().chapter_count == 1
    assert s.query(StudyQuestion).filter_by(id="c10").first().chapter is None
    s.close()


def test_detect_chapters_skips_tiny_documents(db, monkeypatch):
    """Under 8 questions a split leaves chapters too small to practise, and it
    must not cost an AI call."""
    import asyncio

    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study import maintenance as mnt

    s = db()
    s.add(StudyDeck(id="d2", owner="alice", name="Tiny", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="mt", owner="alice", deck_id="d2", name="Short.pdf",
                        kind="pdf", content="x", char_count=1))
    for i in range(3):
        s.add(StudyQuestion(id=f"t{i}", owner="alice", deck_id="d2", material_id="mt",
                            qtype="open", question="q", reference="r", state="new"))
    s.commit()
    s.close()

    called = {"n": 0}

    async def counter(owner, system, user, **kw):
        called["n"] += 1
        return {"chapters": []}

    monkeypatch.setattr(mnt, "_llm_json", counter)
    out = asyncio.run(mnt.run_detect_chapters("alice", "mt"))

    assert out["skipped"] == "too_few_questions"
    assert called["n"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore -k detect`
Expected: FAIL with `AttributeError: module 'routes.study.maintenance' has no attribute 'run_detect_chapters'`

- [ ] **Step 3: Implement `run_detect_chapters`**

In `routes/study/maintenance.py`, after `run_audit_questions`:

```python
# Below this a split leaves chapters too small to practise, and the
# whole-material Practice button already covers the document.
CHAPTER_MIN_QUESTIONS = 8

CHAPTER_SYSTEM = """You map exam/exercise questions to the chapters of the document they came from.

You are given the document text and a numbered list of its questions. Group the questions under the document's OWN chapter or section headings — use the heading text as it appears, prefixed by its number (e.g. "3 — Joint distributions").

Rules:
- Only use headings that genuinely appear in the document. Never invent a structure.
- If the document has no chapter structure — a single exam paper, one problem set — return exactly one chapter covering everything. Do not manufacture divisions.
- Every question id you were given must appear under exactly one chapter.
- `index` is the chapter's position in the document, starting at 1.

Output ONLY JSON: {"chapters": [{"index": 1, "label": "...", "questions": ["id", ...]}, ...]}"""


async def run_detect_chapters(user, material_id: str) -> Dict:
    """Assign each of a material's questions to a chapter of its source document.

    Writes ``chapter``/``chapter_index`` on the questions and ``chapter_count``
    on the material. A document the model reports as one chapter is left
    completely unsplit — that is the rule the picker keys off. Idempotent:
    re-running reassigns rather than duplicating. {chapters, assigned}."""
    db = _common.SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        qq = db.query(StudyQuestion).filter(StudyQuestion.material_id == m.id)
        if user is not None:
            qq = qq.filter(StudyQuestion.owner == user)
        rows = qq.order_by(StudyQuestion.created_at.asc()).all()
        items = [{"id": r.id, "number": r.number,
                  "question": (r.question or "")[:200]} for r in rows]
        content = (m.content or "")[:60000]
    finally:
        db.close()

    if len(items) < CHAPTER_MIN_QUESTIONS:
        return {"chapters": 0, "assigned": 0, "skipped": "too_few_questions"}

    value = await _llm_json(
        user, CHAPTER_SYSTEM,
        f"--- DOCUMENT ---\n{content}\n\n--- QUESTIONS ---\n{json.dumps(items)}",
        temperature=0.2, max_tokens=8000, timeout=180, thinking_off=True)
    chapters = (value or {}).get("chapters") if isinstance(value, dict) else None
    if not isinstance(chapters, list):
        raise HTTPException(502, "Model reply was not a chapter list. Try again.")

    valid = {it["id"] for it in items}
    mapping = {}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        label = str(ch.get("label") or "").strip()
        if not label:
            continue
        try:
            idx = int(ch.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        for qid in ch.get("questions") or []:
            if qid in valid:
                mapping[qid] = (label, idx)

    distinct = {v[0] for v in mapping.values()}
    db = _common.SessionLocal()
    try:
        mat = study_service.get_material(db, material_id, user)
        if len(distinct) < 2:
            # Single chapter: record it and change nothing else.
            mat.chapter_count = 1
            db.commit()
            return {"chapters": 1, "assigned": 0}
        assigned = 0
        for r in db.query(StudyQuestion).filter(
                StudyQuestion.id.in_(list(mapping))).all():
            label, idx = mapping[r.id]
            r.chapter, r.chapter_index = label, idx
            assigned += 1
        mat.chapter_count = len(distinct)
        db.commit()
        return {"chapters": len(distinct), "assigned": assigned}
    finally:
        db.close()
```

If `json` or `HTTPException` are not already imported in this module, they come from `routes.study._common` via its star import — verify with `grep -n "^import json\|HTTPException" routes/study/maintenance.py` and add `import json` at the top if absent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add routes/study/maintenance.py tests/test_study_chapters_themes.py
git commit -m "feat(study): detect document chapters for an existing material"
```

---

## Task 5: Cluster topics into subject-wide themes

**Files:**
- Modify: `routes/study/maintenance.py`
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_cluster_themes_labels_every_question_with_that_topic(db, monkeypatch):
    import asyncio

    from core.database import StudyQuestion
    from routes.study import maintenance as mnt

    _seed_chaptered(db)
    s = db()
    for q in s.query(StudyQuestion).all():
        q.theme = None
    s.commit()
    s.close()

    async def fake(owner, system, user, **kw):
        return {"themes": [
            {"name": "Probability", "topics": ["Bernoulli"]},
            {"name": "Distributions", "topics": ["Covariance"]},
        ]}

    monkeypatch.setattr(mnt, "_llm_json", fake)
    out = asyncio.run(mnt.run_cluster_themes("alice", "d1"))

    assert out["themes"] == 2
    assert out["labelled"] == 6
    s = db()
    assert s.query(StudyQuestion).filter_by(id="c10").first().theme == "Probability"
    # a theme must reach every document carrying that topic
    assert s.query(StudyQuestion).filter_by(id="e0").first().theme == "Distributions"
    s.close()


def test_cluster_themes_is_idempotent(db, monkeypatch):
    """Re-running re-clusters cleanly instead of accumulating."""
    import asyncio

    from core.database import StudyQuestion
    from routes.study import maintenance as mnt

    _seed_chaptered(db)

    async def fake(owner, system, user, **kw):
        return {"themes": [{"name": "Everything", "topics": ["Bernoulli", "Covariance"]}]}

    monkeypatch.setattr(mnt, "_llm_json", fake)
    asyncio.run(mnt.run_cluster_themes("alice", "d1"))
    second = asyncio.run(mnt.run_cluster_themes("alice", "d1"))

    assert second["themes"] == 1
    s = db()
    assert {q.theme for q in s.query(StudyQuestion).all()} == {"Everything"}
    s.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore -k cluster`
Expected: FAIL with `AttributeError: ... has no attribute 'run_cluster_themes'`

- [ ] **Step 3: Implement `run_cluster_themes`**

In `routes/study/maintenance.py`, after `run_detect_chapters`:

```python
THEME_SYSTEM = """You group fine-grained question topics into a handful of coarse study themes.

You are given the topic labels used across one subject. Group them into 6-10 themes a student would recognise as areas of the course ("Integration", "Hypothesis testing"), not restatements of individual topics.

Rules:
- Every topic you were given must appear under exactly one theme.
- Use the topic strings exactly as given — do not reword them.
- Prefer fewer, broader themes over many narrow ones.

Output ONLY JSON: {"themes": [{"name": "...", "topics": ["...", ...]}, ...]}"""


async def run_cluster_themes(user, deck_id: str) -> Dict:
    """Group a subject's topic labels into coarse themes and write them onto
    every question carrying those topics.

    Themes span the subject, so a theme reaches every document that uses the
    topic. Reads no PDFs — it clusters the labels already stored, which is what
    makes it cheap enough to re-run. Idempotent: only ``theme`` is rewritten.
    {themes, labelled}."""
    db = _common.SessionLocal()
    try:
        study_service.get_deck(db, deck_id, user)
        qq = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck_id)
        if user is not None:
            qq = qq.filter(StudyQuestion.owner == user)
        topics = sorted({(r.topic or "").strip() for r in qq.all() if (r.topic or "").strip()})
    finally:
        db.close()

    if not topics:
        return {"themes": 0, "labelled": 0, "skipped": "no_topics"}

    value = await _llm_json(user, THEME_SYSTEM, json.dumps({"topics": topics}),
                            temperature=0.2, max_tokens=4000, timeout=120,
                            thinking_off=True)
    groups = (value or {}).get("themes") if isinstance(value, dict) else None
    if not isinstance(groups, list):
        raise HTTPException(502, "Model reply was not a theme list. Try again.")

    by_topic = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        for t in g.get("topics") or []:
            key = str(t).strip()
            if key in topics:
                by_topic[key] = name

    labelled = 0
    db = _common.SessionLocal()
    try:
        qq = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck_id)
        if user is not None:
            qq = qq.filter(StudyQuestion.owner == user)
        for r in qq.all():
            name = by_topic.get((r.topic or "").strip())
            if name:
                r.theme = name
                labelled += 1
        db.commit()
    finally:
        db.close()
    return {"themes": len(set(by_topic.values())), "labelled": labelled}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add routes/study/maintenance.py tests/test_study_chapters_themes.py
git commit -m "feat(study): cluster subject topics into coarse themes"
```

---

## Task 6: Expose both as routes, Tidy-bank actions and agent tools

**Files:**
- Modify: `routes/study/maintenance.py` (routes inside `register`)
- Modify: `routes/study_routes.py` (shim re-export)
- Modify: `src/study_agent.py` (`maintain_bank`)
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_agent_can_reach_the_new_services(db):
    """The agent reaches the service layer through routes.study_routes; a
    missing re-export breaks a tool at runtime with nothing to catch it."""
    from routes import study_routes as sr

    for name in ("run_detect_chapters", "run_cluster_themes", "groupings_payload"):
        assert hasattr(sr, name), f"routes.study_routes.{name} not re-exported"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore -k agent_can_reach`
Expected: FAIL with `AssertionError: routes.study_routes.run_detect_chapters not re-exported`

- [ ] **Step 3: Add the routes**

In `routes/study/maintenance.py`, inside `register`, beside the other maintenance routes:

```python
    @router.post("/materials/{material_id}/detect-chapters")
    async def detect_chapters(request: Request, material_id: str):
        """Split one document's questions into its own chapters. A document with
        fewer than two headings is left unsplit."""
        return await run_detect_chapters(_owner(request), material_id)

    @router.post("/decks/{deck_id}/group-themes")
    async def group_themes(request: Request, deck_id: str):
        """Cluster the subject's topic labels into coarse, subject-wide themes."""
        return await run_cluster_themes(_owner(request), deck_id)
```

- [ ] **Step 4: Re-export through the shim**

In `routes/study_routes.py`, extend the maintenance import block:

```python
from routes.study.maintenance import (  # noqa: F401
    run_cluster_themes,
    run_detect_chapters,
)
from routes.study.insights import groupings_payload  # noqa: F401
```

If a `from routes.study.maintenance import (...)` block already exists, add the two names to it instead of adding a second block.

- [ ] **Step 5: Extend the agent tool**

In `src/study_agent.py`, in `_maintain_bank`, add before the final `raise`:

```python
    if action == "detect_chapters":
        _require(args, "material_id")
        return await sr.run_detect_chapters(owner, args["material_id"])
    if action == "group_themes":
        _require(args, "deck_id")
        return await sr.run_cluster_themes(owner, args["deck_id"])
```

And update its decorator so the model knows they exist:

```python
@tool("maintain_bank", "Question-bank maintenance: dedup, audit, link_parts, backfill_context, reformat, detect_chapters (split one document's questions into its own chapters), group_themes (cluster a subject's topics into coarse themes). Pass deck_id to confine a pass to one subject; detect_chapters takes material_id.",
      {"action": _s("dedup | audit | link_parts | backfill_context | reformat | detect_chapters | group_themes",
                    enum=["dedup", "audit", "link_parts", "backfill_context", "reformat", "detect_chapters", "group_themes"]),
       "deck_id": _s("Subject id (required for link_parts and group_themes, optional elsewhere)"),
       "material_id": _s("Material id (required for detect_chapters)")}, ["action"])
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py tests/test_study_agent_tools.py -q -p no:cacheprovider -W ignore`
Expected: PASS (14 + 18 passed)

- [ ] **Step 7: Commit**

```bash
git add routes/study/maintenance.py routes/study_routes.py src/study_agent.py tests/test_study_chapters_themes.py
git commit -m "feat(study): routes and agent tools for chapter and theme grouping"
```

---

## Task 7: The practice picker

**Files:**
- Modify: `static/js/study.js` (`renderPractice` front page ~line 2140, `TIDY_ACTIONS` ~line 1705)
- Test: `tests/test_study_chapters_themes_js.py` (create)

- [ ] **Step 1: Add the two Tidy-bank actions**

In `static/js/study.js`, add to the `TIDY_ACTIONS` array:

```js
  { key: 'detect_chapters', label: 'Detect chapters', arm: false, perMaterial: true,
    hint: 'Split each document with several chapters so you can practise one at a time. Documents with a single chapter are left alone. Slow (AI).',
    path: 'detect-chapters', report: r => r.skipped ? 'skipped — too few questions'
      : (r.chapters < 2 ? 'single chapter — not split' : `${r.chapters} chapters, ${r.assigned} questions`) },
  { key: 'group_themes', label: 'Group themes', arm: false,
    hint: 'Cluster this subject’s topics into a handful of themes you can drill across every document. Slow (AI).',
    path: 'group-themes', report: r => `${r.themes} themes over ${r.labelled} questions` },
```

`detect_chapters` is per-material, so in the tidy click handler build its URL as
`/api/study/materials/${matId}/detect-chapters` for each material in the subject,
and `group_themes` as `/api/study/decks/${s.deck.id}/group-themes`.

- [ ] **Step 2: Replace the per-subject Start with the picker**

In `renderPractice`'s no-session branch, change the deck row button from starting immediately to opening the picker:

```js
            <button class="study-btn small" data-prac="${d.id}" ${(d.q_due ?? 0) + (d.q_new ?? 0) === 0 ? 'disabled' : ''}>Choose…</button>
```

and replace the `data-prac` click handler with:

```js
    el.onclick = (e) => {
      const id = e.target.closest('[data-prac]')?.dataset.prac;
      if (id) renderPracticePicker(id);
    };
```

- [ ] **Step 3: Add the picker renderer**

Add above `renderPractice`:

```js
// What to practise, for one subject. Chapters slice one document in its own
// order; themes slice the whole subject by what a question is about. Each
// section is omitted when empty, so a subject of plain exam papers looks
// exactly as it did before this existed.
async function renderPracticePicker(deckId) {
  const el = body();
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  const deck = (S.decks || []).find(d => d.id === deckId);
  let g = { chapters: [], themes: [] };
  try { g = await jget(`/api/study/decks/${deckId}/groupings`); } catch { /* picker still offers Everything */ }
  if (_tab !== 'practice' || S.practice) return;

  const chapterRows = g.chapters.map(doc => `
    <div style="margin-bottom:10px;">
      <div class="study-subtle" style="margin-bottom:4px;">${esc(doc.material)}</div>
      ${doc.chapters.map(c => `
        <div class="study-row">
          <span class="grow">${esc(c.label)}</span>
          <span class="study-badge q">${c.count} q</span>
          <button class="study-btn small" data-chapter="${esc(c.label)}">Practice</button>
        </div>`).join('')}
    </div>`).join('');

  const themeRows = g.themes.map(t => `
    <div class="study-row">
      <span class="grow">${esc(t.name)}</span>
      <span class="study-subtle">${t.count} q · ${t.materials} document${t.materials === 1 ? '' : 's'}</span>
      <button class="study-btn small" data-theme="${esc(t.name)}">Practice</button>
    </div>`).join('');

  el.innerHTML = `
    <div style="max-width:620px;">
      <div class="study-form-row">
        <button class="study-btn small" id="study-pick-back">← Subjects</button>
        <b style="font-size:14px;">${esc(deck?.name || 'Practice')}</b>
      </div>
      <div class="study-rate-row" style="margin:14px 0 6px;">
        <button class="study-btn primary" id="study-pick-all">Everything · ${deck?.q_due ?? 0} due</button>
      </div>
      ${chapterRows ? `<div class="study-section-title" style="margin-top:22px;">By chapter</div>
        <div class="study-subtle" style="margin-bottom:8px;">One document, in its own order.</div>${chapterRows}` : ''}
      ${themeRows ? `<div class="study-section-title" style="margin-top:22px;">By theme</div>
        <div class="study-subtle" style="margin-bottom:8px;">Across every document in this subject.</div>${themeRows}` : ''}
      ${!chapterRows && !themeRows ? `<div class="study-subtle" style="margin-top:18px;">
        No chapters or themes yet — run “Detect chapters” or “Group themes” from Tidy bank in the subject view.</div>` : ''}
    </div>`;

  el.querySelector('#study-pick-back').addEventListener('click', () => { S.practice = null; renderPractice(); });
  el.querySelector('#study-pick-all').addEventListener('click', () => startPractice(deckId));
  el.querySelectorAll('[data-chapter]').forEach(b => b.addEventListener('click', () =>
    startPractice(deckId, 12, { chapter: b.dataset.chapter, label: b.dataset.chapter })));
  el.querySelectorAll('[data-theme]').forEach(b => b.addEventListener('click', () =>
    startPractice(deckId, 12, { theme: b.dataset.theme, label: b.dataset.theme })));
}
```

- [ ] **Step 4: Pass the new scopes through `startPractice`**

In `startPractice`, beside the existing `scope?.materialId` and `scope?.topics` lines:

```js
    if (scope?.chapter) params.set('chapter', scope.chapter);
    if (scope?.theme) params.set('theme', scope.theme);
```

- [ ] **Step 5: Write the structural test**

Create `tests/test_study_chapters_themes_js.py`:

```python
"""Structural guards for the practice picker.

The picker must degrade to exactly today's behaviour when nothing is grouped,
and its section markup must stay in step with the scopes startPractice sends.
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STUDY = (_REPO / "static" / "js" / "study.js").read_text(encoding="utf-8")


def test_picker_sections_are_conditional():
    """Both sections are omitted when empty — a subject of plain exam papers
    must look exactly as it did before chapters existed."""
    assert "chapterRows ?" in _STUDY
    assert "themeRows ?" in _STUDY


def test_picker_scopes_reach_the_queue():
    """A button that sets a scope startPractice never forwards is a dead
    control; both directions must exist."""
    assert "data-chapter=" in _STUDY and "data-theme=" in _STUDY
    assert "params.set('chapter'" in _STUDY
    assert "params.set('theme'" in _STUDY


def test_tidy_bank_offers_both_backfills():
    assert "detect_chapters" in _STUDY and "group_themes" in _STUDY
```

- [ ] **Step 6: Run the checks**

Run: `node --check static/js/study.js && PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes_js.py -q -p no:cacheprovider -W ignore`
Expected: study.js parses; 3 passed

- [ ] **Step 7: Commit**

```bash
git add static/js/study.js tests/test_study_chapters_themes_js.py
git commit -m "feat(study): practice picker for chapters and themes"
```

---

## Task 8: Record chapters during extraction

**Files:**
- Modify: `routes/study/materials.py` (`run_extraction`)
- Test: `tests/test_study_chapters_themes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_extraction_stores_a_chapter_when_one_is_given(db):
    """New extractions carry their chapter straight through, so a freshly added
    workbook is practisable by chapter without a separate backfill pass."""
    from routes.study._common import _question_row_kwargs

    row = _question_row_kwargs({"question": "q", "reference": "r",
                                "chapter": "2 — Limits", "chapter_index": 2})
    assert row["chapter"] == "2 — Limits"
    assert row["chapter_index"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore -k extraction_stores`
Expected: FAIL with `ImportError: cannot import name '_question_row_kwargs'`

- [ ] **Step 3: Add the helper and use it**

In `routes/study/_common.py`:

```python
def _question_row_kwargs(item: Dict) -> Dict:
    """Chapter fields carried from an extracted item onto its StudyQuestion row.

    Kept in one place so extraction and chapter detection agree on the shape;
    both are absent on documents with no chapter structure."""
    try:
        idx = int(item.get("chapter_index") or 0) or None
    except (TypeError, ValueError):
        idx = None
    label = str(item.get("chapter") or "").strip() or None
    return {"chapter": label, "chapter_index": idx}
```

In `routes/study/materials.py`, where `run_extraction` constructs each `StudyQuestion(...)`, add `**_question_row_kwargs(item)` to the constructor call. Locate it with:

`grep -n "StudyQuestion(" routes/study/materials.py`

Then extend the extraction prompt in `src/study_ai.py` so the model returns the fields — find the extraction system prompt and add to its rules:

```
- When the document has chapter or section headings, set "chapter" to the heading the question sits under (as it appears, e.g. "3 — Joint distributions") and "chapter_index" to its position starting at 1. Omit both when the document has no chapter structure.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONUTF8=1 python -m pytest tests/test_study_chapters_themes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (16 passed)

- [ ] **Step 5: Full study regression**

Run: `PYTHONUTF8=1 python -m pytest tests/ -q -p no:cacheprovider -W ignore -k "study" --continue-on-collection-errors`
Expected: 330+ passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add routes/study/_common.py routes/study/materials.py src/study_ai.py tests/test_study_chapters_themes.py
git commit -m "feat(study): carry chapter labels through extraction"
```

---

## Done when

- A multi-chapter document offers its chapters in the picker; a single-chapter one does not appear there at all.
- Themes list across documents and practising one pulls from every document that has it.
- Both backfills are reachable from Tidy bank and from the agent.
- `PYTHONUTF8=1 python -m pytest tests/ -k study` is green.
