# Study: "Teach me" — Socratic teaching sessions in the Tutor tab — design

Date: 2026-11-11
Base: `claude/inspiring-knuth-2y49gd` @ 49ed7b2.

## Why

Jane Street published `socratic-tutor`, an MIT-licensed agent skill
(`janestreet/agent_skills` @ 7c6715c, 2026-11-03): a diagnostic, a skill map
with levels 0-5, one question at a time, spaced review and a final exam. It is
a good method, and close to the loop Study already wants (understand → recall →
problem-solve → feedback → repeat weak points).

It is also prompt-only. The model holds the skill map in its own context and
grades itself, so "levels are earned, not given" depends on the model's
discipline. On long sessions the map drifts, and a model that wants to please
inflates levels. Odysseus has a server, a database and the learner's own
history, so the rules can be code.

## What

A **teaching session** is a Tutor-tab thread whose `tutor_state` column holds
the session as JSON. Start one with **🎓 Teach me** (topic plus optional full
mastery), or ask the ordinary Tutor to teach you. It asks first when that is
unclear, then calls `start_teaching_session`, and the same turn continues as
the tutor.

### Rules the server enforces (`src/study_socratic.py`)

| Rule | Why |
|---|---|
| Placement after the diagnostic is capped at 3/5. Placing any skill above 1/5 needs 3+ answered diagnostic questions. | No skipping the diagnostic. 4/5 ("can do independently") is earned in the loop. |
| A level goes up by one, only after 2 correct checks since the last change, at least one of them independent (unprompted, review, edge case, teach-back). | "Raise a level only on 2+ independent checks, one of them unprompted recall." |
| A check recorded in the turn after `tutor_mark_explained` counts as prompted, whatever kind the model claims. | "Not immediately after the user was told the answer." The model cannot relabel this. |
| A wrong answer resets the streak. A partial answer neither counts nor resets. A failed surprise review drops a level. | Strict grading; spaced review has consequences. |
| 4→5 needs a correct edge case AND a correct teach-back. A drop clears that evidence. | Upstream's level-5 definition. |
| A skill held at 2+ and untested for 4 learner turns becomes **review due**. The prompt tells the tutor to re-test it without warning. The learner's panel never shows the schedule. | Interleaved review is systematic, not left to the model's memory. |
| The final exam opens only when every skill is at the target (4, or 5 for full mastery). It needs one answer per skill plus an integrative problem. It passes only if every final answer was right. A miss drops that skill a level and returns to the loop. | No early exam, and no pass the answers don't support. |

The model reports what the learner did (`tutor_record_check(skill, outcome,
kind)`), and the server decides the level and says what is still missing.

### What a prompt-only tutor cannot do

- **Learner history**: the prompt includes the weakest question-bank topics in
  the focused subject (last 90 days, from `get_weak_question_signals`), so the
  diagnostic aims at real weak spots.
- **Grounding**: questions and short explanations come from the subject's
  materials (`search_materials` / `get_material`) and question bank, with
  citations.
- **Into FSRS**: when the session ends, the tutor offers to save the
  misconceptions and weakest skills as flashcards (`add_cards`, which now keeps
  `tags`), so spaced repetition carries on after the chat, and the exam-plan
  mastery matching picks them up by tag.
- **Persistence**: the session lives with the chat. Upstream's "save state /
  resume from" commands are unnecessary.

### Safety and scope

- Its own tool allowlist (`SOCRATIC_TOOL_NAMES`): read materials and the
  question bank, `add_cards`, and the `tutor_*` tools. No deletes, no edits,
  no code tools, even if the browser sends `allow_code`. Enforced at dispatch.
- Tutor tools get the thread id from dispatch. A `_thread_id` the model writes
  is overwritten, and ownership is checked on every call.
- The UI shows that a tool ran ("· searched your materials"), never its output,
  live or in history: a material snippet or a stored answer would give away the
  question just asked.
- A refused rule saves nothing (load → apply → save in one transaction).

### UI (`static/js/studyAgent.js`)

- **🎓 Teach me** button → topic form → the session opens and sends
  "Teach me <topic>."
- A skill-map panel above the chat: topic, phase (Diagnostic / Learning /
  Final exam / Mastered), target, overall %, a bar per skill with a target tick,
  the focus marker, and the misconceptions spotted. It updates live from
  `tutor_state` SSE events and reloads with the thread.
- Quick commands: Show progress, Harder, Easier, Just tell me (sent as
  messages). Sessions are marked 🎓 in the thread list.

## Not done

- Concurrent turns on one session from two tabs could interleave their
  read-modify-write on the state. The UI sends one turn at a time per tab.
- No Odysseus skill in `SKILL.md` format for external coding agents. That is a
  separate idea.

## Testing

- `tests/test_study_socratic.py`: every rule above, the allowlist, thread-id
  injection and owner isolation, hidden tool outputs, the learner-history
  prompt block, and two full turns through `run_study_agent` with a fake model
  (a session turn, and an ordinary chat switching to the tutor mid-turn).
- `tests/test_study_socratic_panel_js.py`: the real `tutorPanelHtml` under Node
  (levels, target tick, unplaced skills, escaping, mastered state).
