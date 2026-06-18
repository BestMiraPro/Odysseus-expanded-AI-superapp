# Study: explain-further, parallel extraction, history log — design

Date: 2026-06-18
Build order: F2 → F1 → F3 (user priority: explain-further first).

## F2 — "Explain further"

A button that gives a material-grounded theoretical explanation for a practice
question or a flashcard, and points the learner to where the theory lives.

- Surfaces: the practice grade box (after submitting), and a revealed flashcard.
- Output: a Markdown explanation + a "Where to review" footer with the source
  page in the material (the extracted text carries `[Page N]` markers) and the
  relevant section in the AI notes, plus a link that opens the file in the
  in-app viewer at that page (`/api/upload/{file_id}?inline=1#page=N`), reusing
  the Component-C citation style.
- Practice: uses the question's own material text + `material.summary`.
- Cards (no `material_id`): searches the deck's materials/notes, picks the most
  relevant chapter, explains, links there; graceful fallback when a deck has no
  materials/notes (explain from the card's own back/notes, no page link).
- Prompt `EXPLAIN_FURTHER_SYSTEM` returns JSON:
  `{"explanation": "<markdown>", "page": <int|null>, "summary_section": "<str|null>", "material_id": "<str|null>"}`
  (`material_id` only used by the cards path to pick the chapter).
- Caching: new columns `study_questions.deep_explanation` and
  `study_cards.deep_explanation` (guarded migration), with a Regenerate action.
- Model: the text model (study_text_model / DeepSeek) via `_llm_json`, thinking off.

Endpoints:
- `POST /api/study/questions/{id}/explain-further` → `{explanation, page, summary_section, file_id, cached}`
- `POST /api/study/cards/{id}/explain-further` → same shape (file_id from the chosen material)

## F1 — Parallel extraction

Fixes the bug where launching a second extraction orphaned the first.

- `S.subject.extracting` becomes a `Set` of material ids (was a single id).
- A material row shows "Extracting…" only when its id is in the set; all other
  rows stay interactive, so several extractions can run at once.
- Each launch adds its id, fires the request, and on settle removes its id and
  refreshes data; `reloadSubject` leaves the set untouched, so still-running
  extractions keep their indicator. Each request keeps its own retry/error
  handling — one failure doesn't affect the others.
- Frontend-only change.

## F3 — History tab

The data is already logged (`StudyAttempt`, `StudyReview`); this adds a view.

- `GET /api/study/history?limit=...` merges, newest first, owner-scoped:
  - practice attempts joined to their question text, with answer, correct/score,
    AI feedback (from the grading JSON), confidence, hints, timestamp;
  - card reviews joined to the card front, with rating + timestamp.
- Entry shape: `{kind: "question"|"card", when, deck_id, title, outcome, feedback, confidence}`.
- Frontend: add `['history','History']` to the tab bar; `renderHistory()` lists
  entries grouped by day. Read-only.

## Out of scope (v1)

- Editing/deleting history entries.
- Re-ranking which figures/sections are cited (uses the model's choice).
