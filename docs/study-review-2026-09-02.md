# Study app review — 2026-09-02

Scope: the whole Study module (`routes/study_routes.py`, `src/study_*.py`,
`static/js/study.js`, the study tables) plus how it is used to study.
Companion to `docs/superpowers/specs/2026-09-02-study-agent-design.md`.

## 1. What changed in this pass

### New
- **Agent tab** — an in-app agent with 30 study tools (subjects, materials,
  questions, cards, exams, stats, extraction/authoring, transcription, notes,
  overview, bank maintenance) and, for admins who tick *Allow code changes*,
  7 code tools confined to the code root. It tutors from your materials
  (`search_materials` → `get_material` by page → cites "material, p.N").
  Threads persist; "Ask the tutor" buttons in the subject view and after a
  practice answer open it with context.
- **Theory vs practice materials drive the buttons**: theory → *Generate
  questions* (authored), practice → *Extract questions* (faithful, vision by
  default for PDFs when a vision model is configured). *Extract (vision)* is
  gone; forcing a path is still possible through the API (`vision: true|false`).
- **Practice one material** (button on the material row; material filter in
  the question bank) and **practice an exam-plan block** (exams can be linked
  to a subject; Today/Plan blocks get a Practice button that filters by the
  block's topics and falls back to the whole subject).
- **Transcribe (vision OCR)** for scanned / formula-image PDFs so notes,
  consult, search and text extraction can read them. Shown only when the text
  layer is thin.
- Practice keyboard shortcuts (1-9 option, Enter check/next, Ctrl+Enter in the
  answer box); "Generate cards" from a chosen material.

### Bugs fixed
| Bug | Effect before | Fix |
|---|---|---|
| Hard 45 s request timeout applied to LLM-backed study routes not in the exemption list (`/decks/{id}/overview`, `/cards/{id}/explain-further`, `/link-parts`, `/reformat`, `/audit-questions`, `/backfill-context`) | 504 after 45 s; overview generation and card "Explain further" mostly failed | `/api/study/` exempted in `app.py` |
| Focus timer only ticked while the Focus tab was visible | Closing the pane or switching tabs froze the countdown; sessions never auto-finished and overran | Tick is tab-independent; only the clock update needs the DOM |
| Vision extraction silently read only 12 pages | Longer exams lost every question past page 12 | Cap 40, discovery batched in 12-page calls with page offsets, `pages.truncated` reported and shown |
| `question_count` on a material never decreased | Rows showed stale counts after deleting questions | Counted live from the questions table |
| Category change did not re-render the row | New buttons did not appear until reload | Re-render on change |
| `window.confirm()` for deleting subjects/materials/cards/exams and for empty answers | Browsers suppress dialogs after a few ("prevent additional dialogs"); deletes then silently did nothing (the question bank had already moved to armed buttons for this reason) | Armed two-click buttons everywhere; empty-answer submit needs a second click |
| Exam "days left" parsed `YYYY-MM-DD` as UTC | Off by one in negative-offset time zones | Local-midnight parse |
| Notes/overview "generate now?" used `confirm()` | Same suppression problem | Generate button inside the viewer |

### Removed (dead)
- `POST /api/study/ai/quiz` and `POST /api/study/ai/grade` with their prompts
  and request models — v1 leftovers no UI or test used; practice covers both.

### Internals
- Heavy route bodies became module-level service functions
  (`run_extraction`, `run_generate_notes`, `run_generate_overview`,
  `run_transcribe_material`, `practice_queue_payload`, `overview_payload`,
  `history_entries`, `run_dedup`, `run_audit_questions`,
  `run_backfill_context`, `create_material_record`,
  `material_rows_with_counts`) so the agent and the routes share one code path.
- New tables `study_agent_threads`, `study_agent_messages`; new columns
  `study_exams.deck_id`, `study_materials.page_count` (idempotent migration).
- Tests: `tests/test_study_agent_tools.py` (18), `tests/test_study_practice_filters.py` (15).

## 2. Recommendations not implemented (ranked)

1. **Run the app with the repo bind-mounted when using code tools in Docker.**
   Today the container runs the image's baked copy; the agent's edits there
   vanish on rebuild. `docker-compose.yml` now carries a commented mount
   (`.:/app/code`) + `ODYSSEUS_CODE_DIR=/app/code`. Python edits still need a
   restart (`deploy.ps1` builds from `origin/<branch>`, so commit + push first).
2. **Exam mock blocks should launch a timed mock** (fixed count, timer, score
   prediction before marking — the plan text already asks for it). Needs a
   "mock" practice mode: no hints/consult, N questions, summary with predicted
   vs actual.
3. **Calibration dashboard**: the data exists (`confidence` × outcome per
   attempt). A per-subject "sure-but-wrong" rate and a Brier-style score would
   make the calibration habit visible over weeks, not just per session.
4. **Interleave across subjects in "Practice everything"**: the queue
   interleaves topics within the scope but orders due questions by due date
   only; cross-subject round-robin would strengthen interleaving.
5. **Card generation should use the notes**, not the raw material, when notes
   exist (denser, already structured), and default to atomic cloze cards.
6. **`stats` ignores question attempts** (only card reviews feed the daily
   chart and success rate). Fold attempts in so the Focus/History charts
   reflect practice, which is where most retrieval happens now.
7. **Material viewer in-app**: PDFs open in a new browser tab. An in-pane
   viewer (iframe to `/api/upload/{id}?inline=1#page=N`) was blocked by the
   browser earlier; a PDF.js-based viewer would keep consult inside the app.
8. **Reformat/dedup/audit/backfill** have no UI (the agent can run them now
   via `maintain_bank`); a "Tidy bank" button in the subject view would surface
   them for non-agent use.

## 3. Study-method notes (from the data model + how the app is used)

- Practice questions are where the science lives (closed-book retrieval, FSRS
  spacing, interleaving, confidence tagging). Use flashcards only for atomic
  facts/formulas; do not duplicate practice questions as cards.
- Tag confidence every time: "sure but wrong" items are the highest-value
  re-tests the scheduler produces, and they are invisible without the tag.
- Consult before answering costs a hint (schedules sooner). That is right:
  reserve Consult for the feedback phase, then use "Explain further" / the
  tutor to close the gap, and re-attempt when it comes due.
- Mark exam/problem-set materials as Practice and lecture material as Theory:
  the categories now decide extraction vs generation and where
  Consult/Explain-further look for theory.
- Keep the exam plan honest: link exams to subjects so blocks are actionable,
  and treat mock blocks as timed closed-book runs with a score prediction.

## 4. Verification (2026-09-02)

- `PYTHONUTF8=1 python -m pytest` on Windows (host Python 3.14, no Docker): 3147 passed /
  267 failed / 5 errors — the failing set is identical to the pre-change baseline
  (272 entries, all environment-related: missing bcrypt/pyotp/pytest-asyncio, charmap
  collection errors); the 33 new study tests pass. `node --check` passes for
  `study.js` and `studyAgent.js`.
- Click-through on a scratch instance (isolated data dir, auth off) with a mock
  OpenAI-compatible server: subject create + armed delete, paste material →
  Practice/Theory category switch re-renders the row buttons, live question count,
  Practice-from-material with scope chip, `2` + `Enter` keyboard shortcuts, MCQ
  grading, "Ask the tutor" prefill, agent chat streaming with a tool card and
  KaTeX answer, thread reload from the DB, exam ↔ subject link with plan-block
  Practice buttons (Plan + Today), focus timer counting while on another tab.
- Not exercised for real: vision extraction / transcription against a real vision
  model (unit-tested with fakes only) and code tools inside Docker.
