"""A chart must be visible whole, not clipped inside its own scrollbars.

Once plots rendered, they appeared in a box with scrollbars and only part of the
chart showed at a time. Every Run panel carries

    .code-runner-output { max-height:400px; overflow:auto; padding-right:110px }

which is right for a stream of printed text and wrong for a figure: a chart cut
off at 400px can never be seen whole, and the 110px gutter -- reserved for a
Copy pill a figure panel does not have -- only squeezed it further.

These pin the fix and its boundaries. A panel holding a figure is sized by the
figure; the figure scales to the panel's width and most of the viewport's height
so all of it is on screen at once; text-only panels keep their cap; and Study's
own injected image rule must not reintroduce a height that fights it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STYLE = (REPO / "static" / "style.css").read_text(encoding="utf-8")
STUDY_JS = (REPO / "static" / "js" / "study.js").read_text(encoding="utf-8")


def _declarations(css: str, selector: str) -> dict[str, str]:
    """Merged declarations of every rule whose selector list is exactly
    `selector` (whitespace-insensitive)."""
    want = re.sub(r"\s+", "", selector)
    merged: dict[str, str] = {}
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        # Strip comments that precede a rule inside the captured selector text.
        sel = re.sub(r"/\*.*?\*/", "", sel, flags=re.DOTALL)
        if re.sub(r"\s+", "", sel) != want:
            continue
        for decl in body.split(";"):
            if ":" in decl:
                key, value = decl.split(":", 1)
                merged[key.strip()] = value.strip()
    return merged


FIGURE_PANEL = ".code-runner-output:has(> img.code-runner-plot)"


def test_a_panel_with_a_figure_is_not_height_capped():
    decls = _declarations(STYLE, FIGURE_PANEL)
    assert decls, f"no rule for {FIGURE_PANEL}"
    assert decls.get("max-height") == "none", decls


def test_a_panel_with_a_figure_does_not_scroll_internally():
    """The scrollbars in the report were the panel's own."""
    assert _declarations(STYLE, FIGURE_PANEL).get("overflow") == "visible"


def test_a_figure_panel_drops_the_copy_pill_gutter():
    padding = _declarations(STYLE, FIGURE_PANEL).get("padding-right", "")
    assert padding and padding != "110px", padding


def test_the_figure_fits_the_panel_width_keeping_its_aspect_ratio():
    decls = _declarations(STYLE, "img.code-runner-plot")
    assert decls.get("max-width") == "100%"
    assert decls.get("height") == "auto" and decls.get("width") == "auto", (
        "without auto width and height, max-width/max-height distort the chart"
    )


def test_the_whole_figure_fits_on_screen_at_once():
    """Uncapping the panel is not enough on its own: a tall figure at full
    width can still run past the bottom of the window."""
    max_height = _declarations(STYLE, "img.code-runner-plot").get("max-height", "")
    assert max_height.endswith("vh"), max_height
    assert 0 < float(max_height[:-2]) <= 90, max_height


def test_text_only_panels_keep_their_cap():
    """A long stream of printed output should still scroll inside its panel."""
    base = _declarations(STYLE, ".code-runner-output")
    assert base.get("max-height") == "400px"
    assert base.get("overflow") == "auto"


def test_a_note_contains_its_copy_pill():
    """Measured in a browser: an absolutely positioned 32px pill in a one-line,
    22px note (matplotlib's font-cache notice) hung 16px out of the note and
    across the panel's border. As a flex row the note is always at least as
    tall as the pill, whatever size the pill turns out to be."""
    assert _declarations(STYLE, ".code-runner-note").get("display") == "flex"
    pill = _declarations(STYLE, ".code-runner-note .code-runner-copy-inline")
    assert pill.get("position") == "static", pill


def test_long_note_text_wraps_instead_of_pushing_the_pill_out():
    text = _declarations(STYLE, ".code-runner-note .code-runner-pre")
    assert text.get("flex") == "1" and text.get("min-width") == "0", text


def test_the_practice_ask_thread_does_not_clip_a_chart_either():
    """Practice's Ask-AI replies render inside .study-ask-thread, capped at
    260px with its own scroll -- and _enrichRendered runs plot blocks there. A
    chart in a reply would be cut off exactly like the reported one."""
    decls = _declarations(STUDY_JS, ".study-ask-thread:has(img.code-runner-plot)")
    assert decls, "no override lets the Ask-AI thread fit a figure"
    assert decls.get("max-height") == "none"
    assert decls.get("overflow-y") == "visible"


def test_a_text_only_ask_thread_still_scrolls():
    decls = _declarations(STUDY_JS, ".study-ask-thread")
    assert decls.get("max-height") == "260px"
    assert decls.get("overflow-y") == "auto"


def test_studys_own_image_rule_does_not_fight_the_height_limit():
    decls = _declarations(STUDY_JS, ".study-plot-output img.code-runner-plot")
    assert decls, "Study's plot image rule is gone"
    assert "max-height" not in decls and decls.get("height", "auto") == "auto", decls
