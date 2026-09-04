# Study agent + material-aware extraction — implementation plan

**Spec:** `docs/superpowers/specs/2026-09-02-study-agent-design.md`
**Style:** compact plan (single-session execution, token-conscious). Each task
lists files, the change, and its test. Commit only when the owner asks.

## Task 1 — Data model
- `core/database.py`: `StudyAgentThread`, `StudyAgentMessage` models;
  `StudyExam.deck_id`, `StudyMaterial.page_count` columns; extend
  `_migrate_add_study_summary_columns` (idempotent `ALTER TABLE ... ADD COLUMN`).

## Task 2 — Pure helpers (`src/study_ai.py`, `src/study_vision.py`)
- `offset_manifest(manifest, page_offset)`; `should_use_vision(has_pdf, vision_available, explicit)`;
  `TRANSCRIBE_SYSTEM` prompt; `MAX_PAGES = 40`; `render_pdf_pages` returns a
  `(pages, truncated)` aware wrapper `render_pdf_pages_capped`.
- Test: `tests/test_study_practice_filters.py` (helpers section).

## Task 3 — `routes/study_routes.py` refactor + features
- Delete `/ai/quiz`, `/ai/grade`, `QuizIn`, `GradeIn`, `QUIZ_AUTHOR_SYSTEM`, `GRADER_SYSTEM`; fix the module docstring.
- Module-level service functions used by both routes and the agent:
  `create_material_record`, `run_extraction`, `run_generate_notes`,
  `run_generate_overview`, `run_transcribe_material`, `practice_queue_rows`,
  `material_rows_with_counts`, `overview_payload`, `history_entries`.
- Discovery in 12-page batches; `pages`/`pages_truncated` in the extraction response.
- `ExtractIn.vision: Optional[bool] = None` (auto).
- `GET /practice/queue?material_id=&topics=`; `topic_fallback` flag.
- Exam `deck_id` (create/update/dict/overview today blocks).
- `POST /materials/{id}/transcribe`.
- Materials list returns live `question_count`, `page_count`, `thin_text`.
- Test: `tests/test_study_practice_filters.py` (queue section, temp sqlite).

## Task 4 — `src/study_agent.py`
- Tool registry (`ToolSpec(name, schema, handler, destructive, code)`), study
  tool handlers, code-tool bridge via `execute_tool_block(workspace=CODE_ROOT)`,
  `parse_fallback_tool_calls`, `trim_history`, `build_system_prompt`,
  `run_study_agent` SSE generator, thread/message persistence helpers.
- Test: `tests/test_study_agent_tools.py`.

## Task 5 — `routes/study_agent_routes.py` + wiring
- Threads CRUD, messages, `/chat` SSE. Register in `app.py`; add `/api/study/`
  to `_TIMEOUT_EXEMPT_PREFIXES`.

## Task 6 — Frontend
- `static/js/study.js`: category labels + per-category buttons, remove vision
  button, Practice per material + material filter, transcribe button, armed
  deletes (shared helper), focus-tick fix, practice keyboard shortcuts, exam
  subject select + plan-block Practice buttons, "Generate cards" material
  picker, "Ask the tutor" entry points, new "Agent" tab delegating to
  `static/js/studyAgent.js`.
- `static/js/studyAgent.js`: threads, streaming chat, tool cards, scope,
  allow-code toggle.

## Task 7 — Docker note + report
- `docker-compose.yml`: commented opt-in bind mount + `ODYSSEUS_CODE_DIR`.
- `docs/study-review-2026-09-02.md`: bugs, removals, additions, recommendations.

## Task 8 — Verification
- `python -m pytest tests/test_study_*.py -q`; full suite vs baseline;
  `node --check static/js/study.js static/js/studyAgent.js`.
