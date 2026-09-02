# Study: in-app AI agent, material-aware extraction, per-material practice — design

Date: 2026-09-02
Base: `dev` @ da4c6b76 (origin/dev is content-identical).

## Goals

1. An **AI chat inside the Study pane** that can *act*: manage subjects, materials,
   questions, cards, notes and exams through dedicated tools, tutor the learner
   from their own materials, and (for admins, opt-in) **read and change the app's
   code** in the app directory. It carries the app's architecture + the live
   study state so it understands where it is.
2. **Theory materials only generate (author) questions; practice materials only
   extract questions.** The "Extract (vision)" button goes away; extraction uses
   vision by default whenever the material is a PDF and a vision-capable model
   is available.
3. **Practice the questions of one specific material.**
4. An app-wide review: bugs fixed, dead features removed, high-value features
   added, and written recommendations on the app and the user's study method.
5. Token discipline: compact prompts, paged/truncated tool outputs, live state
   summarised (counts, not contents), code-tool schemas only when enabled.

## Non-goals

- Replacing Omnigent (the bundled coding-agent harness) — the study agent is a
  small, focused loop, not a general agent platform.
- Embedding-based retrieval over materials (a grep-style `search_materials`
  tool + paged `get_material` is enough for course-sized corpora).

## 1. Study agent

### 1.1 Architecture

```
static/js/studyAgent.js  --POST /api/study/agent/chat (SSE)--> routes/study_agent_routes.py
   (tab "Agent" in the                                              |
    study pane; threads,                                            v
    messages, tool cards)                                     src/study_agent.py
                                                                |- build_system_prompt(owner, deck_id, allow_code)
                                                                |- TOOLS registry: schema + async handler per tool
                                                                |- run_study_agent(...) -> async SSE generator
                                                                `- persistence: StudyAgentThread / StudyAgentMessage
```

- **Model**: the Study model (`_resolve_study_model(owner, prefer_text=True)`),
  i.e. the text model when configured, else the study model, else utility/default.
  No new settings: the pane's existing model selector governs the agent too.
- **Loop** (`run_study_agent`): at most 15 rounds. Each round streams
  `stream_llm(..., tools=schemas)`; text deltas are forwarded as `{"delta"}`;
  native `tool_calls` are executed sequentially (`tool_start` / `tool_output`
  events); results are appended as OpenAI `tool` messages; the loop ends when a
  round has no tool calls. Fallback for models without native tool calling: a
  fenced `tool_call` JSON block (`{"name", "arguments"}`) or a
  `<tool_call>{...}</tool_call>` block is parsed from the text.
- **Context window**: the last 40 persisted messages of the thread are sent;
  tool results older than the two most recent rounds are trimmed to 300 chars.
  Tool outputs handed to the model are capped at 6 000 chars (the UI event keeps
  up to 20 000 chars).
- **System prompt** = role + rules + a terse *app map* (files, tables, tabs,
  endpoints — a static constant, about 1.2k tokens) + *live state* generated per
  request: subjects with material/question/card counts, the focused subject's
  materials (name, category, kind, chars, question count), model in use, runtime
  (native vs Docker), code root, and whether code tools are enabled.
- **Persistence**: `study_agent_threads(id, owner, title, deck_id, created_at,
  updated_at)` and `study_agent_messages(id, owner, thread_id, role, content,
  tool_calls JSON, tool_call_id, name, created_at)`. Created by
  `Base.metadata.create_all` (new tables, no ALTER needed).

### 1.2 Tools (all owner-scoped)

Study data:
- `list_subjects()`; `create_subject(name, description?)`; `update_subject(deck_id, name?, description?, new_per_day?)`; `delete_subject(deck_id, confirm)`.
- `list_materials(deck_id)`; `get_material(material_id, offset?, limit?)` (paged text, 6k chars per page, keeps `[Page N]` markers); `search_materials(deck_id, query, material_id?)` (case-insensitive substring/regex hits with ±240-char snippets, page numbers, at most 20 hits); `add_material(deck_id, name, text, category?)`; `set_material_category(material_id, category)`; `remove_material(material_id, with_questions?, confirm)`.
- `list_questions(deck_id, material_id?, query?, qtype?, limit?, offset?)` (compact rows); `get_question(question_id)`; `add_questions(deck_id, questions[], material_id?)` (same schema the extractor normalises, so MCQ answers are validated); `update_question(question_id, ...)`; `delete_question(question_id)`; `set_question_suspended(question_id, suspended)`.
- `list_cards(deck_id, query?)`; `add_cards(deck_id, cards[])`; `delete_card(card_id)`.
- `list_exams()`; `create_exam(title, exam_date, topics[], hours_per_week?, deck_id?)`; `generate_plan(exam_id)`.
- `study_stats()` (overview + last 30 history entries, compact).

AI pipelines (reuse the route logic, refactored into module-level functions in `routes/study_routes.py`):
- `extract_questions(material_id, mode='extract'|'author', count?, types?)` — vision auto.
- `transcribe_material(material_id)` — vision OCR for scanned/formula PDFs (new).
- `generate_notes(material_id)`; `get_notes(material_id)`; `generate_overview(deck_id)`.
- `maintain_bank(action='dedup'|'audit'|'link_parts'|'backfill_context', deck_id?)`.

Code (admin only, and only when the request sets `allow_code=true`):
- `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `ls`, `bash` — executed through the existing `execute_tool_block` with the **workspace bound to the code root** (path confinement + sensitive-file deny list apply; bash starts there). `app_info()` returns the app map, code root, runtime mode and how changes take effect.
- Code root: `ODYSSEUS_CODE_DIR` env var, else the app directory (`BASE_DIR`). In Docker that is the container's baked copy: JS/CSS edits are live immediately, Python edits need a restart, and everything is lost on image rebuild unless the repo is bind-mounted (documented in `docker-compose.yml` as an opt-in mount). The agent is told this and must say it when it edits code.

Rules in the prompt: confirm before destructive actions (`confirm=true` only after the user said yes in chat); prefer tools over guessing state; when tutoring, ground answers in `search_materials`/`get_material` and cite material + page; keep replies short; never dump whole materials into the reply.

### 1.3 API

- `GET /api/study/agent/threads` → `[{id, title, deck_id, updated_at}]`
- `POST /api/study/agent/threads {deck_id?}` → thread
- `GET /api/study/agent/threads/{id}/messages` → messages (UI shape)
- `DELETE /api/study/agent/threads/{id}`
- `POST /api/study/agent/chat {thread_id, message, deck_id?, allow_code?}` → SSE:
  `{"delta"}`, `{"type":"tool_start","name","args_preview"}`,
  `{"type":"tool_output","name","output","ok"}`, `{"type":"error","message"}`,
  `data: [DONE]`. Client abort = disconnect; the server stops at the next await.
- `/api/study/` is added to the request-timeout exemptions (LLM-backed routes
  such as overview generation were hitting the 45 s hard timeout — bug).

### 1.4 UI (`static/js/studyAgent.js`, new "Agent" tab)

Left: thread list (new / delete). Main: messages (markdown + KaTeX via `mdToHtml`),
tool cards (collapsed: `name(args)` → expand to output), streaming assistant
bubble, composer (Enter sends, Shift+Enter newline), Stop button, scope select
(current subject / all subjects), and — for admins — an "Allow code changes"
toggle (off by default). Entry points: the tab itself, an "Ask the tutor" button
in the subject view (scopes to that subject) and in practice after answering
(prefills the question).

## 2. Materials: theory vs practice, vision by default

- Category select labels become **Theory** / **Practice (exam / problem set)**;
  stored values stay `theory` / `exam`.
- Row actions: theory → *Generate questions* (author mode); practice → *Extract
  questions* (extract mode). "Extract (vision)" is removed. Both rows keep Open /
  Notes / category / delete; rows with questions get **Practice**.
- `ExtractIn.vision: bool | None = None`. `None` = auto: vision when the material
  has a PDF on disk **and** `_vision_candidates(owner)` is non-empty (else text,
  with the existing thin-text auto-vision and empty-result fallbacks). Explicit
  `true`/`false` still honoured.
- Vision robustness: `MAX_PAGES` 12 → 40; discovery runs in 12-page batches with
  page offsets merged into one manifest; the response reports `pages` and
  `pages_truncated`.
- New `POST /materials/{id}/transcribe`: renders PDF pages and asks the vision
  model for a faithful Markdown transcription (LaTeX math), 3 pages per call,
  writes `content`/`char_count` with `[Page N text]` markers so notes, consult,
  explain-further and text extraction work for scanned PDFs. A **Transcribe**
  button shows on PDF materials whose text layer is thin (`page_count` stored
  on the material; migration adds the column).

## 3. Practice by material / topic

- `GET /practice/queue` gains `material_id` and `topics` (comma-separated,
  case-insensitive substring OR-match on `topic`; if no question matches, the
  filter is dropped and `topic_fallback: true` is returned).
- `startPractice(deckId, limit, {materialId, topics, label})`; the summary's
  "Practice more" keeps the filter; the practice header shows the scope label.
- Material row **Practice** button; question-bank **material filter** select.
- Exams link to a subject: `StudyExam.deck_id` (nullable, migration), editor
  select; plan blocks (Today + Plan tabs) get a **Practice** button that starts
  practice on the linked subject filtered to the block's topics.

## 4. Review outcomes (fixes and removals in this change)

Bugs fixed:
- 45 s hard timeout on LLM-backed study routes not in the exemption list
  (`/decks/{id}/overview`, `/cards/{id}/explain-further`, `/link-parts`, ...).
- Focus timer stops counting when the pane is closed or another tab is open;
  the session never auto-finishes.
- Changing a material's category did not re-render its row.
- `question_count` on materials never decreased when questions were deleted →
  now computed live.
- `confirm()` dialogs (subject/material/card/exam delete, empty answer) can be
  silently suppressed by browsers ("prevent additional dialogs"), making deletes
  look broken → armed two-click buttons everywhere, like the question bank.
- Plan "days left" used UTC parsing of a date-only string (off by one in
  negative-offset time zones).
- Vision extraction silently dropped pages beyond 12.

Removed (dead): `POST /api/study/ai/quiz`, `POST /api/study/ai/grade` and their
prompts/request models — no UI or test used them (practice covers both).

Added: practice keyboard shortcuts (1-9 pick an option, Enter checks / advances,
Ctrl+Enter submits an open answer); "Generate cards" from a chosen material;
transcription; exam–subject link; agent tab.

Written recommendations (not implemented) go in the final report.

## 5. Testing

- `tests/test_study_agent_tools.py` — tool registry/dispatch against a temp
  SQLite DB (subjects, materials, questions round-trips; owner isolation;
  destructive tools require `confirm`; code tools absent unless enabled and
  admin), fenced tool-call fallback parsing, history trimming.
- `tests/test_study_practice_filters.py` — practice queue by material / topics
  (fallback), vision-default decision helper, discovery batch offset merge.
- Existing study tests stay green; the full suite is compared against baseline.
