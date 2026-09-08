"""The Study model picker must be reachable, and must say what it resolved to.

U02 moved the endpoint/model selects into a ``<details id="study-options">``
disclosure so a narrow header could collapse without losing the capability.
Above 900px the stylesheet then hid the *summary*, on the theory that the
controls would sit inline there. They do not: a closed ``<details>`` hides
everything that is not its summary, so hiding the summary hid the whole picker
and left an unlabelled stub in the header.

The visible symptom was a user asking why the Study model is hardcoded. It was
never hardcoded — ``_resolve_study_model`` falls back study -> utility ->
default — but a control you cannot reach and a resolution nobody reports are
indistinguishable from one.

These are structural checks on the stylesheet study.js emits, plus a behaviour
test of the label that now reports the resolution.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._study_js_harness import needs_node, run_js

STUDY_JS = (Path(__file__).resolve().parent.parent
            / "static" / "js" / "study.js").read_text(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent


def _rules_for(selector: str):
    """Every declaration block whose selector list mentions `selector`.

    Pseudo-element rules are excluded: hiding ``::-webkit-details-marker`` is
    a triangle, not the control.
    """
    return [
        m.group(2)
        for m in re.finditer(r"([^{}\n]*)\{([^{}]*)\}", STUDY_JS)
        if selector in m.group(1) and "::" not in m.group(1)
    ]


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------

def test_the_disclosure_summary_is_never_hidden():
    """It is the only way to open the disclosure; hiding it hides the picker."""
    offenders = [
        body for body in _rules_for("#study-options > summary")
        if re.search(r"display\s*:\s*none", body)
    ]
    assert offenders == [], (
        "the Study options summary is hidden by CSS, which makes the model "
        f"picker unreachable at that width: {offenders}"
    )


def test_no_media_query_hides_the_summary_at_any_width():
    """Guards the specific regression: hidden above 900px, shown below."""
    for block in re.findall(r"@media[^{]*\{(.*?)\n\}", STUDY_JS, re.DOTALL):
        if "summary" not in block:
            continue
        assert not re.search(r"summary\s*\{[^}]*display\s*:\s*none", block), (
            f"a media query hides the options summary:\n{block.strip()}"
        )


def test_the_disclosure_opts_out_of_the_global_details_styling():
    """style.css styles every <details> for the inline research disclosures.

    A bare ``details`` selector there sets ``overflow: hidden`` (plus a margin,
    a border and a reveal animation). That clips an absolutely-positioned
    popover to the summary's own box, so the Study disclosure opened onto
    nothing — invisible and unhittable — at every width. The override has to
    be explicit; there is no width at which the global rule stops applying.
    """
    style_css = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    bare_blocks = re.findall(r"(?<![\w.#\-\]])\ndetails\s*\{([^}]*)\}", style_css)
    if not bare_blocks:
        bare_blocks = re.findall(r"(?<![\w.#\-\]])details\s*\{([^}]*)\}", style_css)
    if not any(re.search(r"overflow\s*:\s*hidden", b) for b in bare_blocks):
        pytest.skip("style.css no longer clips every <details>")

    rules = " ".join(_rules_for("#study-options"))
    assert re.search(r"overflow\s*:\s*visible", rules), (
        "the Study options disclosure inherits overflow:hidden from the global "
        "details rule, which clips its popover to the summary's own box"
    )


def test_the_open_popover_is_not_left_transparent_by_the_global_animation():
    """``details[open] > :not(summary)`` runs detail-reveal with fill:both."""
    rules = " ".join(_rules_for("#study-options[open] > :not(summary)"))
    assert re.search(r"animation\s*:\s*none", rules), (
        "the popover still inherits the global reveal animation"
    )


def test_the_model_controls_still_live_in_one_place():
    match = re.search(r"<details[^>]*id=\"study-options\".*?</details>",
                      STUDY_JS, re.DOTALL)
    assert match, "the Study options disclosure is gone from the header"
    block = match.group(0)
    assert 'id="study-ep-select"' in block
    assert 'id="study-model-select"' in block
    assert STUDY_JS.count('id="study-ep-select"') == 1


@pytest.mark.parametrize("control_id", ["study-ep-select", "study-model-select"])
def test_the_selects_carry_visible_labels_in_the_popover(control_id):
    """Stacked in a popover, two bare dropdowns do not say which is which."""
    assert re.search(rf'<label[^>]*for="{control_id}"', STUDY_JS), (
        f"{control_id} has no visible label"
    )


# --------------------------------------------------------------------------
# The label reports the resolution
# --------------------------------------------------------------------------

PRELUDE = """
const API = '';
function mkEl() { return { textContent: '', title: '' }; }
const summary = mkEl();
const note = mkEl();
const _pane = {
  querySelector: (sel) => (sel === '#study-options-summary' ? summary
                        : sel === '#study-model-note' ? note : null),
};
function fakeFetch(body, ok = true) {
  return async () => ({ ok, status: ok ? 200 : 401, json: async () => body });
}
"""


def _label(body, ok=True):
    out = run_js(PRELUDE, "fetchStudyJson", "refreshStudyModelSummary", epilogue=f"""
await refreshStudyModelSummary(fakeFetch({body}, {str(ok).lower()}));
console.log(JSON.stringify({{ label: summary.textContent, title: summary.title,
                              note: note.textContent }}));
""")
    return out


def test_the_label_names_the_configured_study_model():
    out = _label('{ configured: true, source: "study", model: "qwen-72b" }')
    assert "qwen-72b" in out["label"]
    assert "Model" in out["label"]


def test_the_label_names_a_borrowed_model_and_says_it_is_borrowed():
    """A silent fallback is exactly what looked like a hardcoded model."""
    out = _label('{ configured: true, source: "utility", model: "small-fast" }')
    assert "small-fast" in out["label"]
    assert "utility" in out["note"], (
        "the fallback tier is not reported, so an unset Study model is invisible"
    )


def test_the_configured_case_does_not_claim_to_be_borrowing():
    out = _label('{ configured: true, source: "study", model: "m" }')
    assert "borrow" not in out["note"].lower()


def test_no_model_configured_is_stated_plainly():
    out = _label('{ configured: false, source: null, model: null }')
    assert "none" in out["label"].lower()
    assert "Settings" in out["note"]


def test_a_failed_lookup_leaves_a_usable_label():
    """Unauthenticated or offline must not leave the disclosure unlabelled."""
    out = _label('{}', ok=False)
    assert out["label"].strip(), "the disclosure lost its label on an error"
