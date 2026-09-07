"""U02 — model choice must stay reachable on narrow screens.

``.study-model-wrap`` was `display: none` below 900px, which does not move the
control anywhere — it removes the capability. There was no replacement in the
header, so on a phone the study model simply could not be chosen or seen.

These are structural checks against the stylesheet and markup that study.js
emits. Actual rendered appearance at 360px and 768px is D03's job; what is
asserted here is the part source can settle: the control is not removed, it has
a labelled home, and the tab strip scrolls rather than pushing the window
controls off screen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


STUDY_JS = (Path(__file__).resolve().parent.parent
            / "static" / "js" / "study.js").read_text(encoding="utf-8")


def _css_rules_for(selector: str):
    """Every declaration block whose selector list mentions `selector`."""
    return [
        m.group(2)
        for m in re.finditer(r"([^{}\n]*)\{([^{}]*)\}", STUDY_JS)
        if selector in m.group(1)
    ]


def test_the_model_control_is_never_display_none():
    """Hiding it does not relocate the capability; it removes it."""
    offenders = [
        body for body in _css_rules_for(".study-model-wrap")
        if re.search(r"display\s*:\s*none", body)
    ]
    assert offenders == [], (
        f"the model selector is still hidden by CSS: {offenders}"
    )


def test_narrow_screens_get_a_labelled_disclosure_for_study_options():
    assert "study-options" in STUDY_JS, (
        "no narrow-screen home for the model controls"
    )
    assert re.search(r"<summary[^>]*>\s*Study options", STUDY_JS), (
        "the disclosure has no visible label"
    )


def test_the_model_controls_live_inside_that_disclosure():
    """One home for the controls, not a duplicate set for narrow screens."""
    match = re.search(r"<details[^>]*id=\"study-options\".*?</details>",
                      STUDY_JS, re.DOTALL)
    assert match, "the Study options disclosure is not in the header markup"
    block = match.group(0)
    assert 'id="study-ep-select"' in block
    assert 'id="study-model-select"' in block
    assert STUDY_JS.count('id="study-ep-select"') == 1, (
        "the endpoint select is duplicated; there must be exactly one control"
    )
    assert STUDY_JS.count('id="study-model-select"') == 1


def test_the_tab_strip_scrolls_instead_of_wrapping():
    """Wrapping nine tabs pushes the close control off a 360px screen."""
    rules = _css_rules_for(".study-tabs")
    assert rules, ".study-tabs has no rule"
    combined = " ".join(rules)
    assert "overflow-x" in combined, "the tab strip does not scroll horizontally"
    assert not re.search(r"flex-wrap\s*:\s*wrap", combined), (
        "the tab strip still wraps, which grows the header instead of scrolling"
    )


def test_the_window_controls_are_not_shrunk_away():
    """Close and minimize must keep their size when the header is crowded."""
    rules = " ".join(_css_rules_for(".study-x"))
    assert "flex-shrink" in rules and "0" in rules, (
        "the window controls can be squeezed off screen by a crowded header"
    )


@pytest.mark.parametrize("control_id", ["study-ep-select", "study-model-select"])
def test_the_selects_keep_accessible_names(control_id):
    match = re.search(rf'<select id="{control_id}"[^>]*>', STUDY_JS)
    assert match, f"{control_id} is missing"
    assert "aria-label=" in match.group(0), (
        f"{control_id} has no accessible name"
    )
