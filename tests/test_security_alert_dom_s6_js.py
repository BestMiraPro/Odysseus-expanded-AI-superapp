r"""S6 - DOM template sites: bounded values, escaped ids, numeric coercions.

Drives the real functions extracted from the real files for the pure helpers
(notes repeat normalization / checklist markup, calendar time/date form
values) and pins the template-construction sites that are not pure functions
at the source level, in the same convention as the S2 module.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests._study_js_harness import run_js

_REPO = Path(__file__).resolve().parents[1]
_NOTES_JS = _REPO / "static" / "js" / "notes.js"
_CALENDAR_JS = _REPO / "static" / "js" / "calendar.js"
_CALENDAR_UTILS_JS = _REPO / "static" / "js" / "calendar" / "utils.js"
_GALLERY_JS = _REPO / "static" / "js" / "gallery.js"
_THEME_JS = _REPO / "static" / "js" / "theme.js"
_SKILLS_JS = _REPO / "static" / "js" / "skills.js"
_ADJ_POPUP_JS = _REPO / "static" / "js" / "editor" / "fx" / "adj-popup.js"
_COOKBOOK_SERVE_JS = _REPO / "static" / "js" / "cookbookServe.js"
_FILEHANDLER_JS = _REPO / "static" / "js" / "fileHandler.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node binary not on PATH"
)


# ---------------------------------------------------------------------------
# notes #22/#23/#24 - checklist ids escaped; repeat values bounded to the
# canonical enum at the one normalization boundary every render reads.
# ---------------------------------------------------------------------------

_NOTES_PRELUDE = r"""
const uiModule = { esc: (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;') };
"""


@needs_node
def test_notes_repeat_values_are_bounded_to_canonical_forms():
    date = "new Date('2026-05-13T09:00:00')"  # a Wednesday (getDay()=3, day 13)
    vectors = [
        ("none", "none"),
        ("daily", "daily"),
        ("yearly", "yearly"),
        ("weekly:3", "weekly:3"),
        ("monthly:day:17", "monthly:day:17"),
        ("monthly:nth:2:4", "monthly:nth:2:4"),
        ("monthly:last:6", "monthly:last:6"),
        ("weekly", "weekly:3"),                 # legacy bare value derives params
        ("monthly", "monthly:day:13"),
        ("monthly_nth_weekday", "monthly:nth:2:3"),
        ("monthly:day:31", "monthly:day:31"),
        # anything outside the enum must not round-trip into markup
        ("monthly:day:<img src=x>", "none"),
        ("monthly:day:\" onerror=\"x", "none"),
        ("weekly:<script>", "none"),
        ("<img src=x onerror=alert(1)>", "none"),
        ("weekly:7", "none"),
        ("", "none"),
    ]
    prelude = _NOTES_PRELUDE + f"const DATE = {date};\n"
    epilogue = (
        "console.log(JSON.stringify("
        + json.dumps([v for v, _ in vectors])
        + ".map(v => _normalizeRepeat(v, DATE))));\n"
    )
    out = run_js(prelude, "_normalizeRepeat", epilogue=epilogue, source_path=_NOTES_JS)
    assert out == [expected for _, expected in vectors]


@needs_node
def test_notes_checklist_escapes_item_text_and_ids():
    items = '[{ id: \'"><img src=x onerror=1\', text: \'a<b> & "c\', done: false }]'
    prelude = _NOTES_PRELUDE
    epilogue = (
        f"console.log(JSON.stringify(_buildChecklistHtml({items})));\n"
    )
    out = run_js(
        prelude, "_buildChecklistHtml", "_esc", "_uid",
        epilogue=epilogue, source_path=_NOTES_JS,
    )
    assert "<img" not in out
    assert "<b>" not in out
    assert "&lt;b&gt;" in out
    # the hostile id is escaped inside its data- attribute
    assert re.search(r'data-item-id="[^"]*(?:&lt;|&quot;)[^"]*"', out)


def test_notes_custom_date_picker_still_assigns_value_as_property():
    src = _NOTES_JS.read_text(encoding="utf-8")
    assert 'dInput.value = initial;' in src
    assert 'value="${initial}"' not in src


# ---------------------------------------------------------------------------
# calendar #9/#10 - form attribute values must leave the format helpers only
# in exact HH:MM / YYYY-MM-DD shapes (event data can arrive from ICS/CalDAV).
# ---------------------------------------------------------------------------


@needs_node
def test_calendar_fmt_time_bounds_output_to_hhmm():
    vectors = [
        "2026-05-13T09:05:00",
        "2026-05-13T14:30:00",
        "2026-05-13T09:05",
        "<img src=x onerror=alert(1)>",
        "2026-05-13T25:99:00",
        '" onerror="window.__xss=1',
        "x",
        "",
    ]
    prelude = "const payloads = " + json.dumps(vectors) + ";\n"
    epilogue = "console.log(JSON.stringify(payloads.map(p => _fmtTime(p))));\n"
    out = run_js(prelude, "_fmtTime", epilogue=epilogue, source_path=_CALENDAR_JS)

    assert out[0] == "09:05"
    assert out[1] == "14:30"
    assert out[2] == "09:05"
    for rendered in out[3:]:
        assert rendered == ""


@needs_node
def test_calendar_local_date_of_bounds_output_to_iso_date():
    vectors = [
        "2026-05-13",
        "2026-05-13T10:00:00",
        "2026-05-13T10:00:00Z",
        "<img src=x onerror=alert(1)>",
        '"><svg onload=',
        "2026/05/13T10:00",
        "",
    ]
    prelude = "const payloads = " + json.dumps(vectors) + ";\n"
    epilogue = "console.log(JSON.stringify(payloads.map(p => _localDateOf(p))));\n"
    out = run_js(prelude, "_localDateOf", epilogue=epilogue, source_path=_CALENDAR_UTILS_JS)

    assert out[0] == "2026-05-13"
    assert out[1] == "2026-05-13"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", out[2])  # instants resolve in the local tz
    for rendered in out[3:]:
        assert rendered == ""


# ---------------------------------------------------------------------------
# gallery #20 - numeric width/height/gps/size values and escaped album ids.
# ---------------------------------------------------------------------------


def test_gallery_detail_coerces_numeric_metadata():
    src = _GALLERY_JS.read_text(encoding="utf-8")
    assert "${Number(img.width)} x ${Number(img.height)}" in src
    assert "${Number(img.gps.lat)}, ${Number(img.gps.lng)}" in src
    assert 'value="${_esc(a.id)}"' in src
    # _humanSize coerces to a number up front: a non-number cannot reach toFixed
    assert "const size = Number(bytes);" in src
    assert "if (!size) return '';" in src


# ---------------------------------------------------------------------------
# theme #26/#27 - harmony previews no longer build style markup from strings.
# ---------------------------------------------------------------------------


def test_theme_harmony_preview_assigns_css_property_not_markup():
    src = _THEME_JS.read_text(encoding="utf-8")
    assert "span.style.background = c;" in src
    assert "prev.replaceChildren(" in src
    assert "prev.innerHTML = [colors.bg" not in src


# ---------------------------------------------------------------------------
# skills #25 - the only interpolated stats are complete numeric coercions.
# ---------------------------------------------------------------------------


def test_skills_uses_counter_is_a_complete_numeric_coercion():
    src = _SKILLS_JS.read_text(encoding="utf-8")
    assert "const uses = Number(sk.uses) || 0;" in src


# ---------------------------------------------------------------------------
# editor #15 - slider values clamp into their own bounds before markup;
# the tone picker accepts only the three enum groups.
# ---------------------------------------------------------------------------


def test_adj_popup_sliders_clamp_and_tone_is_enum_only():
    src = _ADJ_POPUP_JS.read_text(encoding="utf-8")
    assert "Math.max(min, Math.min(max, Number(value) || 0))" in src
    assert "['shadows', 'midtones', 'highlights'].includes(popEl._cbTone)" in src
    assert "Math.max(-100, Math.min(100, Number(value) || 0))" in src


# ---------------------------------------------------------------------------
# cookbook #14 - derived parser names and saved spec methods are escaped.
# ---------------------------------------------------------------------------


def test_cookbook_serve_panel_escapes_derived_field_values():
    src = _COOKBOOK_SERVE_JS.read_text(encoding="utf-8")
    assert 'data-parser="${esc(_rp_name || \'\')}"' in src
    assert "${esc(_rp_name)}</span>" in src
    assert '<option value="${esc(m)}"' in src


# ---------------------------------------------------------------------------
# fileHandler #18/#19 - img.src sinks receive only locally created File/blob
# URLs; the crop flow and URL revocation stay intact. Narrow dismissal.
# ---------------------------------------------------------------------------


def test_filehandler_img_src_sinks_only_see_local_preview_urls():
    src = _FILEHANDLER_JS.read_text(encoding="utf-8")
    # every img.src assignment traces back to _getPreviewUrl -> createObjectURL
    assert src.count("img.src = ") == 3  # _loadImage probe + crop + chip
    assert "img.src = url;" in src
    assert "img.src = _getPreviewUrl(f);" in src
    assert "URL.createObjectURL(f)" in src
    # addFiles stores the caller-provided File objects (or the cropper's new
    # File(blob)) - no remote URL can enter pendingFiles through it
    assert "pendingFiles.push(nextFile);" in src
    assert "new File([blob]" in src
    # revocation + crop preserved
    assert "URL.revokeObjectURL(url)" in src
    assert "_openMobileCropper" in src
    assert 'finish(new File([blob], `${base}-cropped.${ext}`' in src