# Study Feature — Frontend Analysis

Scope: client-side UI/UX and backend API usage for the Study module in `static/js/study.js`, plus the launcher/integration points in `static/index.html` and `static/app.js`.

---

## 1. UI Surface Inventory & User Flow

The Study panel is a full-screen modal-like overlay injected by `openPanel()` in `static/js/study.js:280`. It shares the generic `modalManager.js` minimize/restore infrastructure, but its DOM is created from scratch inside a fixed-position `.study-pane`. Seven top tabs drive state via `_tab`:

| Tab | File/Function | Purpose |
|-----|---------------|---------|
| **Today** | `study.js:365` `renderToday` | Dashboard. Shows due counts, streak, retrieval count, focus minutes, per-subject action list, and today’s scheduled plan blocks. |
| **Subjects** | `study.js:462` `renderSubjects` | Subject list + creation. Clicking a subject opens the **deck/subject detail view**. |
| **Cards** | `study.js:694` `startReview`, `study.js:1117` `renderReview` | FSRS flashcard player. |
| **Practice** | `study.js:1275` `startPractice`, `study.js:1312` `renderPractice` | Question-bank retrieval practice (MCQ/open-ended). |
| **Plan** | `study.js:1749` `renderPlan` | Exam planner and deterministic schedule viewer. |
| **Focus** | `study.js:1980` `renderFocus` | Pomodoro-style focus timer + 14-day bar chart. |
| **History** | `study.js:330` `renderHistory` | Timeline of card reviews and question attempts. |

### 1.1 Deck/Subject View

`renderSubjectDetail()` (`study.js:573`) is the central content-management screen for a selected subject. It contains:

- Header with back button, subject name, overview button, review/practice buttons.
- **Materials** section: paste text or upload files (`study.js:635`); list with category dropdown (“Theory” vs “Exam/answer key”), Open/Make notes/Extract/Author/Delete actions.
- **Question bank** section: searchable, paginated list with suspend/delete/original-question link.
- **Flashcards** section: manual add form, AI generate from latest material, new-per-day control, proposal list, card list with edit/suspend/delete.

### 1.2 Card Editor

Manual add is a two-textarea form (`study.js:673`). AI-generated cards first render as a checkbox-based **proposals** list (`study.js:895` `renderProposals`) where the user selects which to keep. Per-card edits use the browser `prompt()` API (`study.js:1049`).

### 1.3 Practice/Review Session UIs

- **Cards player** — centered single-card view with a progress bar, question, revealed answer, and four FSRS rating buttons.
- **Practice player** — question type chips, optional problem setup/context, options or free-text textarea, confidence selector (`sure`/`unsure`/`guess`), Hint/Consult/Skip, then AI grading card.
- **Queue** — loaded from `/api/study/queue` and `/api/study/practice/queue`.

### 1.4 Consult / Materials / Notes / Overview Drawer

The same `_viewerShell()` helper (`study.js:762`) is reused for:

- Material study notes (`openMaterialNotes`, `study.js:829`).
- Subject overview (`openSubjectOverview`, `study.js:844`).
- “Explain further” (`openExplainFurther`, `study.js:852`).
- File chooser when multiple sources match (`openFileChooser`, `study.js:879`).
- File viewer opens the upload in a **new browser tab** (`openFileTab`, `study.js:818`), not in-app.

### 1.5 Exam Plan View

`renderPlan()` lists exams; `renderPlanDays()` renders daily blocks with checkbox completion states. `renderExamEditor()` (`study.js:1829`) is an inline CRUD form for exam metadata + topic importance/mastery.

### 1.6 Focus Sessions

Simple timer UI with preset buttons (25/50/90 min), custom input, a large countdown clock, and Finish/Abandon buttons. A 14-day bar chart and recent session list are shown when no timer is active.

### 1.7 Stats

Stats are surfaced in two places:

- **Today tab**: crude chips only (due counts, streak, retrievals, focus minutes).
- **Focus tab**: a 14-day bar chart of focus minutes.

There is no dedicated long-range performance dashboard.

### User Flow Summary

```
Sidebar Study button → Today dashboard
                    → Subjects → create/select subject
                              → upload/paste material
                              → extract/author questions
                              → generate or add cards
                    → Cards → review queue
                    → Practice → question queue
                    → Plan → create exam → generate schedule → complete blocks
                    → Focus → start timer → finish log
                    → History → review recent attempts
```

---

## 2. Backend API Calls & State Management

### 2.1 API Layer

All calls go through `jfetch()` (`study.js:63`), wrapping `fetch()` with same-origin credentials and JSON bodies. Helpers are defined at `study.js:75`:

```js
const jget = (p) => jfetch(p);
const jpost = (p, body) => jfetch(p, { method: 'POST', body: JSON.stringify(body || {}) });
const jput  = (p, body) => jfetch(p, { method: 'PUT',  body: JSON.stringify(body || {}) });
const jdel = (p) => jfetch(p, { method: 'DELETE' });
```

Base URL is `window.location.origin` (`study.js:17`). Errors are extracted from `detail` or `error` fields; otherwise the HTTP status is shown.

### 2.2 Endpoints Called

| Endpoint | Method | Where | Purpose |
|----------|--------|-------|---------|
| `/api/model-endpoints` | GET | `study.js:312` | populate AI endpoint selector |
| `/api/auth/settings` | GET/POST | `study.js:314`, `study.js:333` | save study model |
| `/api/study/history` | GET | `study.js:342` | activity feed |
| `/api/study/overview` | GET | `study.js:373` | Today tab counts/plan |
| `/api/study/decks` | GET/POST | `study.js:480`, `study.js:519` | subject list / create |
| `/api/study/decks/:id` | PUT | `study.js:655` | new-per-day setting |
| `/api/prefs/study_school` | GET/PUT | `study.js:524` | localization pref |
| `/api/prefs/study_order` | GET/PUT | `study.js:536` | ordering pref |
| `/api/study/decks/:id/cards` | GET/POST/PUT | `study.js:590`, `study.js:677`, `study.js:715` | cards |
| `/api/study/decks/:id/materials` | GET/POST | `study.js:590`, `study.js:609` | materials |
| `/api/study/decks/:id/questions` | GET | `study.js:590` | question bank |
| `/api/upload` | POST | `study.js:644` | file upload |
| `/api/study/materials/:id` | DELETE | `study.js:971` | remove material |
| `/api/study/materials/:id/category` | PUT | `study.js:950` | theory/exam category |
| `/api/study/materials/:id/extract` | POST | `study.js:1007` | extract/author questions from material |
| `/api/study/materials/:id/notes` | GET/POST | `study.js:829` | material study notes |
| `/api/study/ai/generate-cards` | POST | `study.js:709` | AI card proposals |
| `/api/study/decks/:id/overview` | GET/POST | `study.js:844` | subject overview doc |
| `/api/study/cards/:id/explain-further` | POST | `study.js:852` | theory lookup for card |
| `/api/study/questions/:id/explain-further` | POST | `study.js:852` | theory lookup for question |
| `/api/study/questions/:id` | PUT/DELETE | `study.js:1074` | suspend/delete question |
| `/api/study/questions/:id/locate` | POST | `study.js:891` | consult source location |
| `/api/study/questions/:id/hint` | POST | `study.js:1448` | hint request |
| `/api/study/questions/:id/ask` | POST | `study.js:1469` | AI Socratic chat |
| `/api/study/questions/:id/explain` | POST | `study.js:1533` | explain MCQ options |
| `/api/study/questions/:id/attempt` | POST | `study.js:1511` | grade/submit practice answer |
| `/api/study/questions/:id/prereqs` | GET | `study.js:1354` | multi-part question chain |
| `/api/study/queue` | GET | `study.js:1101` | card review queue |
| `/api/study/practice/queue` | GET | `study.js:1297` | practice question queue |
| `/api/study/cards/:id/review` | POST | `study.js:1244` | submit FSRS rating |
| `/api/study/exams` | GET/POST | `study.js:1766`, `study.js:1913` | exam CRU |
| `/api/study/exams/:id` | PUT/DELETE | `study.js:1913`, `study.js:1791` | exam update/delete |
| `/api/study/exams/:id/generate-plan` | POST | `study.js:1783` | build schedule |
| `/api/study/exams/:id/toggle-block` | POST | `study.js:435`, `study.js:1871` | complete plan block |
| `/api/study/focus/start` | POST | `study.js:2041` | start focus session |
| `/api/study/focus/:id/finish` | POST | `study.js:2053` | end focus session |
| `/api/study/focus/recent` | GET | `study.js:2002` | recent focus history |
| `/api/study/stats` | GET | `study.js:2002` | daily stats for chart |

### 2.3 State Management Approach

The module uses a single module-scoped mutable object `S` (`study.js:19`). All UI state lives in this bag:

```js
const S = {
  overview: null,
  decks: [],
  subject: null,
  review: null,
  practice: null,
  exams: [],
  examEditing: null,
  focus: null,
  focusHistory: [],
};
```

There is no framework, no reactive binding, no URL route state, and no undo/redo. Re-rendering is done by manually clearing `innerHTML` and rebuilding listeners (`setTab()` at `study.js:388`, individual `render*` functions).

This design is simple but fragile:
- Multiple parallel async operations can race against global `_tab` checks (`if (_tab !== 'xyz') return;`) — correct but ad-hoc.
- Review/practice objects carry UI-only transient state (e.g., `r.revealed`, `p.choice`, `p.answerDraft`, `p.askBusy`) mixed with session metadata.
- The active file viewer is a singleton tracked only by DOM id `#study-viewer`; closing it relies on `study.js:294` reading from `_pane || document`.

### 2.4 How Review Ratings Are Submitted

Cards use a 4-button FSRS rating scale (`Again`/`Hard`/`Good`/`Easy` mapped to `1–4`) in `study.js:1194`:

```js
el.querySelectorAll('.study-rate').forEach(b =>
  b.addEventListener('click', () => rateCard(parseInt(b.dataset.r, 10))));
```

When a button is clicked, `rateCard()` at `study.js:1224`:

1. Advances `r.idx`, resets `revealed`, and queues failed cards (`rating === 1` or `rating <= 2` for new cards) to the end of the session for immediate re-drill.
2. Sends the network request asynchronously:

```js
await jpost(`/api/study/cards/${card.id}/review`, { rating, duration_ms: duration });
```

3. If the network request fails, a toast informs the user (`study.js:1242`) but the UI has already moved on. **There is no retry mechanism, no offline queue, and no rollback of the optimistic UI advancement.**

Cards are also re-queued locally when rated `Again` (`rating === 1`) or `<= 2` for new cards (`study.js:1232`), which keeps the session alive but can cause the same card to appear multiple times. No server-side re-queue confirmation is awaited.

---

## 3. Practice/Review UX in Detail

### 3.1 Cards (FSRS) Player

- **Presentation**: One card at a time, centered, large front text, smaller back text after revealing. Math/KaTeX is rendered via `mdToHtml()`.
- **Reveal**: Spacebar or a primary “Show answer (Space)” button.
- **Rating**: Four horizontally laid-out buttons with labels and per-button interval previews from the server (`card.preview[n]`).
- **Keyboard**: `Space` to reveal, `1–4` to rate.
- **Explain further**: available after reveal.
- **Session end**: summary chips for Again/Hard/Good/Easy counts plus success-rate tips.

This supports retrieval practice reasonably well: closed-book first, active recall, immediate feedback, and FSRS scheduling. However:
- **No escape from the queue when a card is wrong** other than immediately re-drilling it.
- **No audio/image support** in the visible UI; the code paths expect text/markdown only.
- **No typing/hard-input mode**: pure self-assessment ratings are subject to hindsight bias.

### 3.2 Practice (Question Bank) Player

- **Presentation**: question type chip + topic + difficulty + progress bar. Context block shown if `q.context` exists. Pre-requisite chain fetched lazily (`study.js:1354`) for multi-part items.
- **MCQ**: clickable buttons for each option; selected option highlighted; after submission correct/wrong styles applied. Open-ended: textarea.
- **Confidence**: `sure`/`unsure`/`guess` buttons required before checking. The UI records confidence but does not prevent grading; backend may or may not use it.
- **Hints**: up to three hints requested from `/api/study/questions/:id/hint` (`study.js:1448`). Each hint is displayed as a blockquote-like banner. `hints_used` sent in the attempt payload includes hints + consult.
- **Consult**: first click triggers `/api/study/questions/:id/locate` (`study.js:891`). If a source location is returned, a second click opens the file/URL. Consulting before answering sets `p.consulted = true`, which penalizes the attempt as if a hint was used.
- **Ask AI**: a chat thread below the question. Before answering, the AI is instructed not to give the answer (Socratic). After answering, it becomes a tutor.
- **Grading feedback**: returned score (0–100), verdict, prose feedback, follow-up probe, reference, and next interval.
- **Navigation**: Next button re-adds failed (`rating === 1`) questions to the end of the session for re-drill.

### 3.3 Retrieval-Practice Support

Strengths:
- Closed-book first for both cards and practice questions.
- Consult/hint cost is transparent to the user and penalized in scheduling.
- Confidence calibration is explicitly surfaced and logged.
- Failed items are re-drilled in the same session.

Weaknesses:
- MCQ allows recognition instead of recall. Without an “I answered from memory” gate, users can select answers by elimination and still mark Good.
- The AI open-ended grader is asynchronous; slow networks cause the submit button to hang with only a text change.
- Confidence is a 3-point word scale, not a probability, so calibration tracking is coarse.
- After a wrong MCQ answer, the system immediately shows the correct option; there is no enforced “type the correct answer” step to strengthen memory.

---

## 4. UX, Accessibility, Mobile, Code Quality & Bugs

### 4.1 UX Issues

- **No progress dashboards beyond the immediate session.** Streak and daily retrieval count are visible but not trended. No per-topic mastery chart, no “weak topics over time” view.
- **Plan tab lacks daily/weekly integration.** Exam blocks are just checkboxes; there is no connection to Focus sessions, no auto-logging of completed study time.
- **Focus sessions are isolated.** Starting focus does not link to a subject, deck, question, or plan block, so there is no attribution of Pomodoro minutes to scheduled work.
- **No bulk operations** on questions or cards (bulk suspend, bulk delete, bulk import/export).
- **Search is client-side only** in the subject detail, and there is no global cross-subject search.
- **AI model selector** is hidden at <900 px, so mobile users cannot change study AI models.
- **All card edits use native `prompt()`** (`study.js:1049`), which cannot handle multi-line text, has poor UX, and is blocked in some browser environments.
- **Toast messages** can stack and overlap; there is no rate limiting or deduplication (`study.js:84`).

### 4.2 Accessibility Issues

- Many buttons are `<div>`s or plain `<button>`s without `aria-label`, `aria-pressed`, or live regions.
- Rating buttons rely on color alone (red/green/blue borders) to convey meaning.
- The progress bar is a `<div><i>` with no `role="progressbar"`, `aria-valuenow`, etc.
- No skip-link, no focus trap, no `aria-modal` on `.study-pane`.
- Modal close on `Escape` is implemented but no announcement is made to screen readers.
- Math/KaTeX output may not be readable by screen readers without `aria-label` or MathML alternatives.
- Inputs lack visible focus rings in the injected CSS; focus states are inherited only from generic browser styles.

### 4.3 Mobile Issues

- The pane becomes full-screen on narrow viewports (`@media (max-width: 768px) { .study-pane { inset: 0; border-radius: 0; } }`), which is good, but the tab bar wraps and can consume significant header space.
- The model selector is hidden at 900 px (`study.js:133` CSS), reducing discoverability.
- Card and practice players are centered but button rows wrap; on very small screens rating buttons become vertically stacked, which is acceptable but not optimized.
- File upload, PDF open-in-new-tab, and markdown viewers work on mobile but feel desktop-first.
- Touch targets for small action buttons (the `✕` delete buttons) are likely too small (~22×22 px).

### 4.4 Code Quality Issues

| Issue | Evidence |
|-------|----------|
| Global mutable state | `S` object at `study.js:19`; `_open`, `_pane`, `_tab`, `_keyHandler` globals. |
| Large monolithic file | `study.js` is 2,119 lines with UI, API, state, CSS injection, and event wiring. |
| Long functions | `renderPractice()` spans `study.js:1312–1565`; `renderSubjectDetail()` spans `study.js:573–691`. |
| Repetition | `_viewerShell()` and `_renderMarkdownInto()` exist but card-vs-practice explain paths still duplicate open/close scaffolding. |
| Inline HTML strings | Almost all rendering uses template-literal `innerHTML`, making XSS escaping easy to miss and static analysis hard. |
| Memory leaks | `setInterval` for focus timer (`study.js:1999`) is cleared, but event listeners inside re-rendered `innerHTML` are recreated each render; this is mitigated by delegated handlers but not systematically. |
| Hardcoded limits | Card list capped at 200 items (`study.js:1036`), question pagination jumps by fixed 40 (`study.js:1052`). |
| Mixed concerns | Practice session object contains UI state (`askBusy`, `explainBusy`, `hintBusy`, `consultBusy`) plus data (`log`, `queue`). |
| No modules for sub-features | All of cards, practice, focus, plan, history live in one file. |

### 4.5 Likely Bugs

1. **Optimistic card advance without rollback** (`study.js:1224–1244`). If the network fails, the user already saw the next card and lost the chance to correct the rating.
2. **Race in reloadSubject** (`study.js:595`). `Promise.all` fires three requests; if the user navigates away mid-load, partial state updates can leave `S.subject` in an inconsistent state.
3. **Focus timer drift** (`study.js:1999` uses 500 ms interval). The clock can lag or overshoot; it relies on `Date.now() - startTs`, but the visual tick is not frame-synced.
4. **`_keyHandler` registered globally** remains active even when the Study pane is minimized, but it checks `_tab === 'review'` so impact is limited. Closing/minimize paths could be clearer.
5. **`consultAction` race**: `p.consultBusy` is set, `renderPractice` is called, then the async locate runs. If user clicks rapidly, the UI flickers.
6. **`S.practice` null on consult** not validated? `consultAction` checks `if (!p || p.consultBusy) return;`, so it is safe.
7. **No duplicate prevention on subject creation**: double-clicking “Create subject” can fire multiple requests.
8. **File upload button** triggers extraction without requiring a material name; if name is empty the backend receives `null`.
9. **Reset on back navigation**: browser back/forward does not restore Study tab state; it just stays in the SPA but the panel may be closed.

---

## 5. Learning-Experience Gaps

From a UX perspective, the Study feature covers the core mechanics but lacks the scaffolding that turns mechanics into habit:

- **No onboarding / empty-state guidance**: Today tab tells totals, but a new user does not know what to do first. Subjects tab has a brief line, but no checklist.
- **No streak/badges/gamification beyond the bare streak counter** in Today. No weekly goals, no “study every day for X days” celebration.
- **Weak feedback loops**: after a session, the summary is mostly numbers. There is no “what to do next” recommendation, no auto-suggested focus session, no direct link to missed items.
- **No progress dashboard**: retention curves, per-topic accuracy, calibration graphs over time, expected exam-readiness scores are all absent.
- **No keyboard shortcuts for Practice**: keyboard supports only card review (`Space`, `1–4`). Practice MCQ options have no `1/A`, `2/B` hotkeys; submit, hint, consult, and next require the mouse.
- **No offline support**: all state is server-generated; a dropped connection ends the session with toasts.
- **No spaced-repetition preview**: the user cannot see future due dates or forecast workload without starting a session.
- **No integrated Pomodoro planning**: Focus timer and Plan blocks do not talk to each other.
- **No community/sharing**: no shared decks, no leaderboard, no collaborative exam planning.
- **No analytics/teacher view**: for an educator persona, there is no class-level or cohort-level view.
- **No accessibility statement**: the feature is not keyboard-first and offers no screen-reader optimized mode.

---

## 6. Prioritized Improvement Opportunities

### P0 — Fix correctness & data-loss risks

1. **Make card review rating durable**: do not advance UI until the `/review` request succeeds, or queue failed ratings and retry (`study.js:1230–1244`).
2. **Add request deduplication / loading locks** for Create subject, extract, generate-plan, and generate-cards to prevent double submissions.
3. **Persist practice session state locally** so a page reload does not lose progress and answers.

### P1 — Retrieve-practice hardening

4. **Add a typed-recall mode for cards**: optionally require the user to type a keyword/answer before revealing the back, then self-rate.
5. **Force “try again” on wrong MCQ**: require typing the correct rationale before moving to Next.
6. **Replace 3-word confidence with a numeric confidence slider** (0–100) and show calibration graphs over time.
7. **Keyboard shortcuts for Practice**: number keys 1–n select MCQ options, `Enter` submit, `H` hint, `C` consult, `N` next.

### P2 — Dashboard & habit support

8. **Build a real Stats dashboard**: retention curve, due forecast, per-topic accuracy, calibration (confidence vs. outcome), focus-time trend.
9. **Link Focus sessions to Plan blocks/decks**: when starting a focus session, ask “What are you working on?” and offer today’s plan blocks.
10. **Add a weekly goal + streak celebration** surfaced in Today and on session completion.

### P3 — UX polish & accessibility

11. **Replace native `prompt()` card edits** with an inline edit form (`study.js:1049`).
12. **Add ARIA roles and keyboard focus management** to the Study pane: `aria-modal`, focus trap, `role="progressbar"`, visible focus styles.
13. **Increase touch targets** and add swipe gestures for card rating on mobile.
14. **Add a global cross-subject search** and bulk operations for questions/cards.
15. **Consistent error boundary**: catch render failures and show a fallback instead of silently blank areas.

### P4 — Architecture

16. **Split `study.js` into focused modules** (`study/api.js`, `study/cards.js`, `study/practice.js`, `study/plan.js`, `study/focus.js`) to reduce duplication and improve testability.
17. **Introduce a small reactive state wrapper** or at least explicit render subscriptions, so individual components can re-render without rebuilding the whole tab.
18. **Add a service-worker/offline queue** for card ratings and focus finishes.

---

*Written as part of the Study feature upgrade analysis. Scope: frontend UI/UX and API usage only.*
