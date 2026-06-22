# Study: consult materials during practice — design

Date: 2026-06-18

## Problem

The Study module lets users build question banks from uploaded papers, but:
1. Uploaded PDFs are silently truncated to 15,000 chars, so long chapters lose
   most of their content (and extraction/summaries only ever see the first part).
2. There is no way to review a subject's material while practising — no
   summary/notes and no way to open the original files inside the app.

## Goals

- Store the full text of uploaded study materials (no chat-oriented cap).
- Generate AI study notes per chapter and a short subject overview, with
  figures pulled from the source PDFs, for consultation.
- Let users open their uploaded files inside the app.
- Make consultation available during practice in a way that respects the
  science of retrieval practice.

## Learning-science basis (for the during-practice rule)

Closed-book retrieval is the "desirable difficulty" that drives the testing
effect; looking answers up mid-question reduces the deep processing that builds
memory. Feedback *after* an attempt is where consultation helps. Therefore:

- During an active question (before submit): consulting counts like a hint
  (increments `hints_used`, which the FSRS mapping already turns into a
  "Hard" / sooner-scheduled outcome).
- After submit (feedback phase): free.
- Outside practice (subject review): free.

Refs: Roediger & Karpicke (testing effect); open- vs closed-book testing
(Frontiers 2019); feedback & the testing effect (PMC6700364).

## Components (build order A → B → C → D)

### A. Remove the 15k cap for study materials
- `src/document_processor.py::_process_pdf` gains `max_chars: int | None = 15000`
  (unchanged default keeps chat behaviour). Study's `_extract_file_text`
  passes `max_chars=None` for full text.
- Add a "re-extract text" action on a material so existing (truncated)
  materials can be refreshed without re-uploading.

### B. In-app file viewer
- `GET /api/upload/{file_id}` gains `?inline=1` → `FileResponse(..., content_disposition_type="inline")` so PDFs render in the browser instead of downloading. Auth unchanged.
- Frontend: a "Files" list in the subject view; clicking opens a viewer
  drawer/modal that embeds the file (native browser PDF rendering). Same viewer
  is reachable from the practice Consult drawer (D).

### C. AI study notes with figures
- DB: `StudyMaterial.summary` (Text, per-chapter notes as markdown),
  `StudyDeck.overview` (Text, subject overview markdown).
- Figures: extract raster images from each PDF page (size-filtered, source page
  recorded); a vision pass selects the figures relevant to each notes section.
  Selected figures are saved as uploaded files and embedded in the markdown by
  URL (`/api/upload/{id}?inline=1`), each captioned with its source page and a
  link that opens that page in the viewer (B). Vector-only figures that can't be
  extracted are cited + linked to the page instead of embedded.
- Generation: manual "Generate notes" per material (text model = study_text_model
  / DeepSeek; figure relevance = vision model / Kimi) and "Generate overview"
  per subject. Manual, to control token spend; regenerating overwrites.
- Frontend: Notes/Overview UI in the subject view (the "consult" section,
  alongside Files from B).

### D. Practice Consult drawer
- Practice view gets a Consult button opening tabs [Overview | This chapter's
  notes | Files].
- Opening it before submitting an answer increments the attempt's `hints_used`
  (existing penalty path → sooner scheduling). After submit and outside
  practice, it is free.

## Data model summary

- `StudyMaterial.summary` (Text, nullable)
- `StudyDeck.overview` (Text, nullable)
- Figures reuse the existing uploads store; no new table.

## Out of scope (v1)

- Cropping bounding boxes for vector figures (link-to-page instead).
- Auto-regeneration of notes on upload (manual only).
