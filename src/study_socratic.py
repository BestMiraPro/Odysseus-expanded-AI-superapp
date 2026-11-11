# src/study_socratic.py
"""Socratic teaching sessions for the Study Tutor tab: the learner model.

A teaching session takes one topic from the learner's current level to
mastery: a diagnostic, a skill map with levels 0-5, one question at a time,
surprise re-tests of earlier skills, and a final mixed exam.

The pedagogy is adapted from Jane Street's ``socratic-tutor`` agent skill
(https://github.com/janestreet/agent_skills, MIT, see ACKNOWLEDGMENTS.md).
That skill is prompt-only: the model keeps the skill map in its own head and
grades itself. Here the map lives on the server and the evaluation rules are
code, so a model cannot hand out levels, skip the diagnostic, or start the
final exam early:

- placement after the diagnostic is capped at 3/5, and placing anyone above
  1/5 needs at least ``MIN_DIAGNOSTIC_TURNS`` answered diagnostic questions;
- a level goes up by one, and only after ``RAISE_CORRECT`` correct checks
  since the last change, at least one of them independent recall;
- a check recorded right after the tutor explained the skill counts as
  prompted recall, whatever the model calls it;
- a failed surprise review, or a missed final-exam question, drops the level;
- 5/5 needs a handled edge case AND a teach-back;
- the final exam opens only when every skill is at the target, and the
  session passes only if every final answer was right.

The model reports what the learner did; this module decides the level.

This module is pure (no DB, no network): ``state`` is a plain dict stored as
JSON on the thread (``StudyAgentThread.tutor_state``); every function takes
it, mutates it, and returns a result dict for the model. Errors raise
``TutorError`` with a message written for the model to act on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LEVEL_LABELS = (
    "unaware",
    "heard of it",
    "can follow along",
    "can do with help",
    "can do independently",
    "can teach it / handle edge cases",
)
MAX_LEVEL = 5
DEFAULT_TARGET = 4
MAX_PLACEMENT = 3          # diagnostic placement ceiling; 4-5 must be earned
MIN_DIAGNOSTIC_TURNS = 3   # answered diagnostic questions before placing above 1
MIN_SKILLS, MAX_SKILLS = 2, 10
RAISE_CORRECT = 2          # correct checks since the last change, to go up one
REVIEW_GAP_TURNS = 4       # a skill untested this many turns is due a surprise review
REVIEW_MIN_LEVEL = 2       # only re-test skills the learner has started to hold
MISCONCEPTIONS_PER_SKILL = 6
HISTORY_PER_SKILL = 12
NAME_CHARS = 60
NOTE_CHARS = 200

PHASES = ("diagnostic", "teaching", "final", "complete")
OUTCOMES = ("correct", "partial", "wrong")
# Kinds the model may claim while teaching. "diagnostic" and "final" are set
# by the phase, not by the model.
TEACHING_KINDS = ("prompted", "unprompted", "review", "edge_case", "teach_back")
INDEPENDENT_KINDS = ("unprompted", "review", "edge_case", "teach_back")
DIFFICULTY_MIN, DIFFICULTY_MAX = -2, 2


class TutorError(ValueError):
    """A rule refused the call; the message tells the model what to do instead."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# construction + lookup
# ---------------------------------------------------------------------------

def new_state(topic: str, *, full_mastery: bool = False) -> Dict[str, Any]:
    topic = _clip(topic, 120)
    if not topic:
        raise TutorError("A teaching session needs a topic.")
    return {
        "version": 1,
        "topic": topic,
        "phase": "diagnostic",
        "target": MAX_LEVEL if full_mastery else DEFAULT_TARGET,
        "difficulty": 0,
        "turn": 0,
        "focus": None,
        "outline": [],
        "skills": [],
        "diagnostic_turns": [],
        "final_integrative": None,
        "result": None,
        "started_at": _now_iso(),
    }


def _new_skill(key: str, name: str) -> Dict[str, Any]:
    return {
        "key": key, "name": name, "level": 0, "placed": False,
        "correct_since": 0, "independent_since": 0,
        "edge_ok": False, "teach_ok": False,
        "checks": 0, "wrong": 0,
        "last_checked_turn": None, "taught_turn": None,
        "final": [], "misconceptions": [], "history": [],
    }


def find_skill(state: Dict, ref: Any) -> Dict:
    """Skill by key ("s1") or name (case-insensitive, then unique prefix)."""
    want = _clip(ref, NAME_CHARS).lower()
    skills = state.get("skills") or []
    if not want:
        raise TutorError("Name the skill.")
    for s in skills:
        if s["key"].lower() == want or s["name"].lower() == want:
            return s
    hits = [s for s in skills if s["name"].lower().startswith(want)]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(s["name"] for s in skills) or "(no skill map yet — call tutor_set_skill_map)"
    raise TutorError(f"Unknown skill '{ref}'. Skills: {names}.")


def _require_phase(state: Dict, *phases: str) -> None:
    if state.get("phase") not in phases:
        raise TutorError(f"Not allowed in the {state.get('phase')} phase "
                         f"(only during: {', '.join(phases)}).")


def begin_turn(state: Dict) -> int:
    """Count one learner message. Review spacing and the 'just explained'
    rule are measured in these turns."""
    state["turn"] = int(state.get("turn") or 0) + 1
    return state["turn"]


# ---------------------------------------------------------------------------
# session start: skill map, diagnostic, placement
# ---------------------------------------------------------------------------

def set_skill_map(state: Dict, skills: List[Any], *, outline: Optional[List[Any]] = None) -> Dict:
    """Define the subskills (diagnostic phase only, before placement).

    Re-defining keeps the diagnostic evidence of skills whose name is kept."""
    _require_phase(state, "diagnostic")
    names: List[str] = []
    seen = set()
    for raw in skills or []:
        name = _clip(raw.get("name") if isinstance(raw, dict) else raw, NAME_CHARS)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    if not MIN_SKILLS <= len(names) <= MAX_SKILLS:
        raise TutorError(f"Give {MIN_SKILLS}-{MAX_SKILLS} distinct, concrete subskills "
                         f"(got {len(names)}). For a broad topic, map the first branch and "
                         "list the other branches in outline.")
    old = {s["name"].lower(): s for s in state.get("skills") or []}
    out = []
    for i, name in enumerate(names, 1):
        s = old.get(name.lower()) or _new_skill(f"s{i}", name)
        s["key"], s["name"] = f"s{i}", name
        out.append(s)
    state["skills"] = out
    state["outline"] = [_clip(b, NAME_CHARS) for b in (outline or []) if _clip(b, NAME_CHARS)][:12]
    return {"skills": [s["name"] for s in out], "outline": state["outline"],
            "next": "Tell the learner you will start with a few questions, one at a time, "
                    "then ask the first diagnostic question."}


def place(state: Dict, levels: Dict[str, Any], *, focus: Optional[str] = None) -> Dict:
    """End the diagnostic: set starting levels and move to teaching."""
    _require_phase(state, "diagnostic")
    if not state.get("skills"):
        raise TutorError("Call tutor_set_skill_map first.")
    wanted: Dict[str, int] = {}
    for ref, lvl in (levels or {}).items():
        s = find_skill(state, ref)
        try:
            wanted[s["key"]] = int(lvl)
        except (TypeError, ValueError):
            raise TutorError(f"Level for '{ref}' must be a number 0-{MAX_PLACEMENT}.")
    diag_turns = len(set(state.get("diagnostic_turns") or []))
    if any(v > 1 for v in wanted.values()) and diag_turns < MIN_DIAGNOSTIC_TURNS:
        raise TutorError(
            f"Run the diagnostic first: {diag_turns} answered diagnostic question(s) so far, "
            f"{MIN_DIAGNOSTIC_TURNS} needed before placing any skill above 1/5. Ask the next "
            "question and record the answer with tutor_record_check.")
    capped = []
    for s in state["skills"]:
        lvl = max(0, wanted.get(s["key"], 0))
        if lvl > MAX_PLACEMENT:
            capped.append(s["name"])
            lvl = MAX_PLACEMENT
        s["level"], s["placed"] = lvl, True
        s["correct_since"] = s["independent_since"] = 0
        s["history"].append({"turn": state["turn"], "event": "placed", "level": lvl})
    state["phase"] = "teaching"
    state["focus"] = (find_skill(state, focus)["key"] if focus
                      else min(state["skills"], key=lambda s: s["level"])["key"])
    res = {"phase": "teaching", "focus": _skill_name(state, state["focus"]), "map": render_map(state),
           "next": "Show the learner this map, propose where to start, and let them override."}
    if capped:
        res["capped"] = (f"Placement is capped at {MAX_PLACEMENT}/5 for: {', '.join(capped)}. "
                         f"4/5 and up are earned in the loop.")
    return res


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def _needs(state: Dict, s: Dict) -> str:
    """What the skill still needs to go up one level (for the model)."""
    lvl = s["level"]
    if lvl >= MAX_LEVEL:
        return "at 5/5"
    if lvl == MAX_LEVEL - 1:
        missing = [n for n, ok in (("an edge case/adversarial question (edge_case)", s["edge_ok"]),
                                    ("a teach-back (teach_back)", s["teach_ok"])) if not ok]
        return ("ready for 5/5 on the next correct independent check" if not missing
                else "for 5/5 still needs " + " and ".join(missing))
    more = max(0, RAISE_CORRECT - s["correct_since"])
    parts = []
    if more:
        parts.append(f"{more} more correct check(s)")
    if not s["independent_since"]:
        parts.append("one of them independent (not right after a hint)")
    return f"for {lvl + 1}/5 needs " + ", ".join(parts) if parts else f"ready for {lvl + 1}/5"


def _raise_ready(s: Dict) -> bool:
    if s["level"] >= MAX_LEVEL:
        return False
    if s["correct_since"] < RAISE_CORRECT or s["independent_since"] < 1:
        return False
    if s["level"] == MAX_LEVEL - 1:
        return bool(s["edge_ok"] and s["teach_ok"])
    return True


def _set_level(state: Dict, s: Dict, level: int, why: str) -> None:
    prev = s["level"]
    s["level"] = max(0, min(MAX_LEVEL, level))
    s["correct_since"] = s["independent_since"] = 0
    if s["level"] < prev or s["level"] < MAX_LEVEL - 1:
        # A drop means the edge-case / teach-back evidence is stale too.
        s["edge_ok"] = s["teach_ok"] = False
    s["history"].append({"turn": state["turn"], "event": why, "level": s["level"]})


def record_check(state: Dict, skill: Any, outcome: str, kind: Optional[str] = None, *,
                 note: Optional[str] = None, misconception: Optional[str] = None) -> Dict:
    """Record one graded learner answer and apply the level rules."""
    phase = state.get("phase")
    if phase == "complete":
        raise TutorError("The session is complete. Start a new teaching session to continue.")
    outcome = str(outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        raise TutorError("outcome must be correct, partial or wrong "
                         "(vague or half-right answers are partial).")
    s = find_skill(state, skill)
    turn = int(state.get("turn") or 0)
    prev = s["level"]
    res: Dict[str, Any] = {"skill": s["name"]}

    if misconception:
        m = _clip(misconception, NOTE_CHARS)
        if m and m.lower() not in (x.lower() for x in s["misconceptions"]):
            s["misconceptions"] = (s["misconceptions"] + [m])[-MISCONCEPTIONS_PER_SKILL:]

    s["checks"] += 1
    s["last_checked_turn"] = turn
    if outcome == "wrong":
        s["wrong"] += 1

    if phase == "diagnostic":
        eff = "diagnostic"
        state.setdefault("diagnostic_turns", [])
        if turn not in state["diagnostic_turns"]:
            state["diagnostic_turns"].append(turn)
        n = len(set(state["diagnostic_turns"]))
        res["next"] = (f"{n} diagnostic question(s) answered. Do not teach yet. "
                       + ("Ask the next one, easy to hard." if n < MIN_DIAGNOSTIC_TURNS
                          else "Ask 1-2 more if a skill is still unclear, then tutor_place."))
    elif phase == "final":
        eff = "final"
        s["final"].append(outcome)
        if outcome != "correct":
            _set_level(state, s, s["level"] - 1, "missed in final exam")
        res["final_answered"] = sum(1 for x in state["skills"] if x["final"])
        res["final_total"] = len(state["skills"])
    else:  # teaching
        eff = str(kind or "unprompted").strip().lower()
        if eff not in TEACHING_KINDS:
            raise TutorError(f"kind must be one of: {', '.join(TEACHING_KINDS)}.")
        taught = s.get("taught_turn")
        if eff in INDEPENDENT_KINDS and taught is not None and turn - taught <= 1:
            res["downgraded"] = ("Counted as prompted: you explained this skill in the last turn, "
                                 "so this is not independent recall yet. Re-test it later.")
            eff = "prompted"
        if outcome == "correct":
            s["correct_since"] += 1
            if eff in INDEPENDENT_KINDS:
                s["independent_since"] += 1
            if eff == "edge_case" and s["level"] >= MAX_LEVEL - 1:
                s["edge_ok"] = True
            if eff == "teach_back" and s["level"] >= MAX_LEVEL - 1:
                s["teach_ok"] = True
            if _raise_ready(s):
                _set_level(state, s, s["level"] + 1, "earned")
        elif outcome == "wrong":
            if eff == "review":
                _set_level(state, s, s["level"] - 1, "failed review")
                res["next"] = "Failed review: the level dropped. Revisit this skill now."
            else:
                s["correct_since"] = s["independent_since"] = 0
        # partial: neither counts nor resets — probe further.

    s["history"] = (s["history"] + [{"turn": turn, "kind": eff, "outcome": outcome,
                                     "note": _clip(note, NOTE_CHARS) or None}])[-HISTORY_PER_SKILL:]
    res.update({"kind": eff, "outcome": outcome, "level": s["level"], "previous_level": prev,
                "changed": s["level"] != prev, "needs": _needs(state, s)})
    if s["level"] != prev and phase == "teaching":
        res["say"] = f"Tell the learner in one line: {s['name']} → {s['level']}/5."
    if phase == "teaching" and all(x["level"] >= state["target"] for x in state["skills"]):
        res["ready_for_final"] = ("Every skill is at the target. Tell the learner, then "
                                  "tutor_start_final for the final mixed assessment.")
    return res


def mark_explained(state: Dict, skill: Any) -> Dict:
    """The tutor gave the answer away for this skill: the next check of it
    counts as prompted recall, and the evidence streak restarts."""
    _require_phase(state, "teaching", "final")
    s = find_skill(state, skill)
    s["taught_turn"] = int(state.get("turn") or 0)
    s["correct_since"] = s["independent_since"] = 0
    s["history"] = (s["history"] + [{"turn": s["taught_turn"], "event": "explained"}])[-HISTORY_PER_SKILL:]
    return {"skill": s["name"],
            "next": "Re-test the same idea from a different angle in a later turn; "
                    "the answer right after this explanation counts as prompted."}


def set_focus(state: Dict, skill: Any) -> Dict:
    _require_phase(state, "teaching")
    s = find_skill(state, skill)
    state["focus"] = s["key"]
    return {"focus": s["name"], "level": s["level"], "needs": _needs(state, s)}


def adjust_difficulty(state: Dict, direction: str) -> Dict:
    d = str(direction or "").strip().lower()
    if d not in ("harder", "easier"):
        raise TutorError("direction must be harder or easier.")
    cur = int(state.get("difficulty") or 0)
    new = max(DIFFICULTY_MIN, min(DIFFICULTY_MAX, cur + (1 if d == "harder" else -1)))
    state["difficulty"] = new
    res = {"difficulty": new, "label": difficulty_label(new)}
    if new == cur:
        res["note"] = f"Already at the {'hardest' if d == 'harder' else 'easiest'} setting."
    return res


def difficulty_label(d: int) -> str:
    return {-2: "much easier", -1: "easier", 0: "normal", 1: "harder", 2: "much harder"}.get(int(d or 0), "normal")


# ---------------------------------------------------------------------------
# final assessment
# ---------------------------------------------------------------------------

def start_final(state: Dict) -> Dict:
    _require_phase(state, "teaching")
    below = [f"{s['name']} ({s['level']}/5)" for s in state["skills"] if s["level"] < state["target"]]
    if below:
        raise TutorError(f"Not every skill is at the target {state['target']}/5 yet. "
                         f"Below target: {', '.join(below)}. Keep teaching those.")
    state["phase"] = "final"
    state["final_integrative"] = None
    for s in state["skills"]:
        s["final"] = []
    return {"phase": "final",
            "next": "Run the final mixed assessment, one question at a time: at least one "
                    "question per skill (record each with tutor_record_check) and one "
                    "integrative problem. Then tutor_finish."}


def finish(state: Dict, *, integrative_correct: bool, summary: Optional[str] = None) -> Dict:
    """Grade the final assessment from the recorded answers.

    The server decides the result: every skill needs at least one final
    answer, every final answer must be correct, and so must the integrative
    problem. A fail goes back to the teaching loop (missed skills already
    dropped a level when the answer was recorded)."""
    _require_phase(state, "final")
    missing = [s["name"] for s in state["skills"] if not s["final"]]
    if missing:
        raise TutorError(f"Ask at least one final question on: {', '.join(missing)}; "
                         "record each answer, then call tutor_finish.")
    state["final_integrative"] = bool(integrative_correct)
    missed = [s["name"] for s in state["skills"] if any(o != "correct" for o in s["final"])]
    passed = not missed and bool(integrative_correct) and \
        all(s["level"] >= state["target"] for s in state["skills"])
    weakest = [s["name"] for s in sorted(state["skills"],
                                         key=lambda s: (s["level"], -s["wrong"], -len(s["misconceptions"])))[:3]]
    state["result"] = {"passed": passed, "weakest": weakest, "missed": missed,
                       "integrative_correct": bool(integrative_correct),
                       "summary": _clip(summary, 600) or None, "at": _now_iso()}
    if passed:
        state["phase"] = "complete"
        state["focus"] = None
        nxt = ("Passed. Show the final map (tutor_progress) and a short summary of the weakest "
               "areas. Offer to save the misconceptions and weakest skills as flashcards "
               "(add_cards) so spaced repetition keeps them alive.")
    else:
        state["phase"] = "teaching"
        first = next((s for s in state["skills"] if s["name"] in missed), None) \
            or min(state["skills"], key=lambda s: s["level"])
        state["focus"] = first["key"]
        nxt = ("Not passed" + (f": missed {', '.join(missed)}" if missed else "")
               + ("" if integrative_correct else "; integrative problem not solved")
               + ". Tell the learner plainly, then go back to the loop on the weakest skill.")
    return {"passed": passed, "weakest": weakest, "missed": missed, "phase": state["phase"],
            "map": render_map(state), "next": nxt}


# ---------------------------------------------------------------------------
# reading the state
# ---------------------------------------------------------------------------

def _skill_name(state: Dict, key: Optional[str]) -> Optional[str]:
    for s in state.get("skills") or []:
        if s["key"] == key:
            return s["name"]
    return None


def review_due(state: Dict) -> Optional[Dict]:
    """The skill most overdue for a surprise re-test, or None.

    Only in the teaching phase, only for skills the learner has started to
    hold (level >= 2) and that are not the current focus."""
    if state.get("phase") != "teaching":
        return None
    turn = int(state.get("turn") or 0)
    cands = [s for s in state.get("skills") or []
             if s["key"] != state.get("focus") and s["level"] >= REVIEW_MIN_LEVEL
             and s.get("last_checked_turn") is not None
             and turn - s["last_checked_turn"] >= REVIEW_GAP_TURNS]
    if not cands:
        return None
    s = min(cands, key=lambda x: x["last_checked_turn"])
    return {"skill": s["name"], "turns_since": turn - s["last_checked_turn"]}


def overall(state: Dict) -> int:
    skills = state.get("skills") or []
    if not skills:
        return 0
    return round(100 * sum(s["level"] for s in skills) / (MAX_LEVEL * len(skills)))


def _bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(width * fraction)))
    return "█" * filled + "░" * (width - filled)


def render_map(state: Dict) -> str:
    """The ASCII skill map (Jane Street's format), for the chat."""
    skills = state.get("skills") or []
    if not skills:
        return f"Topic: {state.get('topic')}\n(no skill map yet)"
    width = max(14, min(NAME_CHARS, max(len(s["name"]) for s in skills)))
    head = f"Topic: {state.get('topic')}"
    pct = overall(state)
    lines = [f"{head:<{width + 18}} overall: {_bar(pct / 100)} {pct}%",
             "-" * (width + 40)]
    for s in skills:
        if not s["placed"]:
            mark = "  (not placed yet)"
            lvl = "?/5"
        else:
            lvl = f"{s['level']}/5"
            mark = ""
            if s["level"] >= state["target"]:
                mark = "  * " + ("mastered" if s["level"] >= MAX_LEVEL else "independent")
            if s["key"] == state.get("focus"):
                mark = "  <- current focus"
        lines.append(f"{s['name']:<{width}}  [{_bar(s['level'] / MAX_LEVEL)}]  {lvl}{mark}")
    lines.append("-" * (width + 40))
    return "\n".join(lines)


def public_state(state: Optional[Dict]) -> Optional[Dict]:
    """What the skill-map panel shows. Leaves out the review schedule (a
    re-test should come without warning) and the per-check notes."""
    if not state:
        return None
    return {
        "topic": state.get("topic"),
        "phase": state.get("phase"),
        "target": state.get("target"),
        "difficulty": state.get("difficulty") or 0,
        "difficulty_label": difficulty_label(state.get("difficulty") or 0),
        "overall": overall(state),
        "focus": _skill_name(state, state.get("focus")),
        "outline": state.get("outline") or [],
        "skills": [{"name": s["name"], "level": s["level"], "placed": s["placed"],
                    "label": LEVEL_LABELS[s["level"]], "focus": s["key"] == state.get("focus"),
                    "misconceptions": list(s["misconceptions"])}
                   for s in state.get("skills") or []],
        "result": state.get("result"),
    }


def prompt_state(state: Dict) -> str:
    """The session block of the system prompt: authoritative, compact."""
    lines = [f"TEACHING SESSION — topic: {state.get('topic')}",
             f"Phase: {state.get('phase')} · target {state.get('target')}/5 · difficulty "
             f"{difficulty_label(state.get('difficulty') or 0)} · learner turn {state.get('turn') or 0}"]
    if state.get("outline"):
        lines.append("Later branches (not in this map): " + "; ".join(state["outline"]))
    skills = state.get("skills") or []
    if skills:
        lines.append("Skills:")
        for s in skills:
            lvl = f"{s['level']}/5" if s["placed"] else "unplaced"
            tag = " [FOCUS]" if s["key"] == state.get("focus") else ""
            extra = ""
            if state.get("phase") == "teaching" and s["placed"]:
                extra = f" — {_needs(state, s)}"
            if state.get("phase") == "final":
                extra = f" — final answers: {', '.join(s['final']) or 'none yet'}"
            lines.append(f"- {s['name']}: {lvl}{tag}{extra}")
            if s["misconceptions"]:
                lines.append("  misconceptions seen: " + " | ".join(s["misconceptions"][-3:]))
    else:
        lines.append("Skills: none yet.")
    if state.get("phase") == "diagnostic":
        lines.append(f"Diagnostic questions answered: {len(set(state.get('diagnostic_turns') or []))}.")
    due = review_due(state)
    if due:
        lines.append(f"SPACED REVIEW DUE: {due['skill']} (untested for {due['turns_since']} turns). "
                     "Make your next question a surprise re-test of it (no warning), "
                     "record it with kind=review, then return to the focus.")
    if state.get("result") and state.get("phase") != "complete":
        r = state["result"]
        lines.append(f"Last final exam: not passed (missed: {', '.join(r.get('missed') or []) or '—'}).")
    return "\n".join(lines)
