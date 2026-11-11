"""The teaching-session skill map panel in the Tutor tab (studyAgent.js).

Runs the real ``tutorPanelHtml`` under Node: levels and the target marker,
unplaced skills, escaping of model-written names, and the quick commands that
disappear once the session is mastered.
"""

from __future__ import annotations

import json

from tests._study_js_harness import REPO, extract_const, needs_node, run_js

pytestmark = needs_node

AGENT_JS = REPO / "static" / "js" / "studyAgent.js"


def _panel(state) -> str:
    src = AGENT_JS.read_text(encoding="utf-8")
    prelude = "\n".join(extract_const(n, src) for n in ("TUTOR_PHASES", "TUTOR_COMMANDS"))
    prelude += ("\nconst esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')"
                ".replace(/>/g, '&gt;').replace(/\"/g, '&quot;');")
    epilogue = f"console.log(JSON.stringify(tutorPanelHtml({json.dumps(state)}, esc)));"
    return run_js(prelude, "tutorPanelHtml", epilogue=epilogue, source_path=AGENT_JS)


def _state(**kw):
    base = {"topic": "Elasticity", "phase": "teaching", "target": 4, "overall": 35,
            "difficulty": 0, "difficulty_label": "normal", "result": None,
            "skills": [
                {"name": "Definition", "level": 4, "placed": True, "label": "can do independently",
                 "focus": False, "misconceptions": []},
                {"name": "Computing", "level": 2, "placed": True, "label": "can follow along",
                 "focus": True, "misconceptions": ["Uses slope instead of % change"]},
            ]}
    base.update(kw)
    return base


def test_no_session_renders_nothing():
    assert _panel(None) == ""


def test_levels_target_and_focus():
    html = _panel(_state())
    assert "🎓 Elasticity" in html and "Learning" in html and "target 4/5" in html
    assert 'class="study-tutor-row done"' in html          # 4/5 meets the target
    assert 'class="study-tutor-row focus"' in html and "▸ Computing" in html
    assert 'style="width:40%"' in html and 'style="left:80%"' in html
    assert "Misconceptions spotted (1)" in html
    assert 'data-tutor-cmd="just tell me"' in html


def test_unplaced_skills_show_a_question_mark():
    st = _state(phase="diagnostic", skills=[{"name": "A", "level": 0, "placed": False, "label": "unaware",
                                             "focus": False, "misconceptions": []}])
    html = _panel(st)
    assert "Diagnostic" in html and '<span class="lv">?</span>' in html


def test_model_written_names_are_escaped():
    st = _state(topic='<img src=x onerror=alert(1)>')
    st["skills"][0]["name"] = "<script>x</script>"
    html = _panel(st)
    assert "<img" not in html and "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_mastered_session_drops_the_commands():
    html = _panel(_state(phase="complete", result={"passed": True, "weakest": ["Computing"]}))
    assert "Mastered" in html and "data-tutor-cmd" not in html
    assert "weakest: Computing" in html


def test_an_empty_map_says_it_is_being_built():
    assert "breaking the topic into skills" in _panel(_state(phase="diagnostic", skills=[]))
