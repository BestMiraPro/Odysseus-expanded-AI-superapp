# Study: reformat old text to LaTeX + multi-part alínea context — design

Date: 2026-06-20
Build order: P1 (reformat) → P2 (multi-part context).

## P1 — Reformat existing AI text to LaTeX

All 180 existing questions (and cards) were generated before the LaTeX/markdown
update: they use plain notation (g_y, D_1, 2*3) with no `$…$`, and the new
markdown renderer mangles the raw `*`/`_` (e.g. 2*3 → "23", D_1 → italics).

- `REFORMAT_SYSTEM` prompt: convert math to LaTeX (`$…$` inline, `$$…$$`
  display) and fix formatting, preserving wording, numbers, options and answers
  EXACTLY — no content/meaning changes. JSON in / JSON out, keyed by id.
- `POST /api/study/reformat`: for the caller's decks, batch questions
  (question, options, reference) and cards (front, back), reformat via the text
  model, write back. Skip any item that already contains `$` (idempotent); keep
  the original text if a batch fails or an id goes missing. correct_index and
  all non-text fields are never touched.
- Run once over existing data; the endpoint stays for future use.

## P2 — Multi-part "alíneas" dependency context

Multi-part exam questions are split into standalone parts at extraction; later
parts (16b, 16c…) sometimes need an earlier part's info/answer.

- DB: `StudyQuestion.number` (part label, e.g. "16b") and `prereq_ids` (JSON
  list of question ids this part depends on). Guarded migration.
- New extractions persist `number` (already produced by extraction).
- Detection: `POST /api/study/decks/{id}/link-parts` runs per material — the AI
  is given the material's questions (id + text), assigns/confirms each part
  label, and (cross-checking the alíneas) lists, for each later part, the ids of
  earlier parts whose info/answer it needs. Stored in number + prereq_ids.
- Practice: a question's queue item flags `has_prereqs`; a
  `GET /api/study/questions/{id}/prereqs` returns each prerequisite part's
  question, the user's latest answer to it, and the correct answer. The practice
  view shows them in an "Earlier in this problem" context box above the question.

## Out of scope (v1)
- Cross-material dependencies (parts are linked within one material).
- Auto re-running link detection on every extraction (manual/endpoint-triggered).
