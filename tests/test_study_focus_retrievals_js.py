"""The Focus tab's retrievals chart referenced undefined variables.

``renderFocus`` built a "Last 14 days (retrievals)" chart from ``retr[i]`` and
``maxRetr``, neither of which was ever defined. study.js is an ES module, so
that is a ReferenceError: opening the Focus tab threw and the chart never
drew. Found by opening every Study tab in a real browser and reading the
console — the same class of defect as B01.

These drive the real computation rather than asserting on source text.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import extract_function, needs_node, run_js

pytestmark = needs_node


def _run(daily, epilogue_extra=""):
    prelude = f"""
const last14 = {daily};
const maxMin = Math.max(30, ...last14.map(d => d.focus_min));
"""
    # Lift just the two derivations out of renderFocus so they can be exercised
    # without the surrounding DOM.
    src = extract_function("renderFocus")
    lines = [ln for ln in src.splitlines()
             if "const retr =" in ln or "const maxRetr =" in ln]
    assert len(lines) == 2, f"expected retr and maxRetr derivations, found {lines}"
    body = "\n".join(ln.strip() for ln in lines)
    script = prelude + body + f"""
{epilogue_extra}
console.log(JSON.stringify({{
  retr,
  maxRetr,
  heights: last14.map((d, i) => Math.round((retr[i] / maxRetr) * 100)),
}}));
"""
    return run_js(script, epilogue="")


def test_retrievals_sum_attempts_and_reviews():
    out = _run("[{focus_min:0,attempts:3,reviews:2},{focus_min:0,attempts:1,reviews:0}]")
    assert out["retr"] == [5, 1]
    assert out["maxRetr"] == 5


def test_missing_fields_count_as_zero():
    """Older stats rows omit one or both counters."""
    out = _run("[{focus_min:0},{focus_min:0,attempts:4},{focus_min:0,reviews:6}]")
    assert out["retr"] == [0, 4, 6]


def test_bar_heights_are_finite_percentages():
    out = _run("[{focus_min:0,attempts:10,reviews:0},{focus_min:0,attempts:5,reviews:0}]")
    assert out["heights"] == [100, 50]


def test_an_empty_fortnight_does_not_divide_by_zero():
    """maxRetr floors at 1, so a quiet fortnight is 0%, not NaN%."""
    out = _run("[{focus_min:0,attempts:0,reviews:0},{focus_min:0}]")
    assert out["maxRetr"] >= 1
    assert out["heights"] == [0, 0]
    assert all(isinstance(h, int) for h in out["heights"])


def test_the_chart_expression_has_its_inputs_defined():
    """The defect itself: the chart used names the function never bound."""
    src = extract_function("renderFocus")
    assert "retr[i]" in src, "the retrievals chart is gone"
    assert "const retr =" in src, "retr is used but never defined"
    assert "const maxRetr =" in src, "maxRetr is used but never defined"
