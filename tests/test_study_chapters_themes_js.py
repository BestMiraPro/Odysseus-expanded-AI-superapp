"""Structural guards for the practice picker.

Browser behaviour has no unit coverage, so these pin the two invariants that
would silently break it: sections must be conditional (a subject with nothing
grouped has to look exactly as it did before), and every scope a button sets
must actually be forwarded to the queue or the button is a dead control.
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STUDY = (_REPO / "static" / "js" / "study.js").read_text(encoding="utf-8")


def test_picker_sections_are_conditional():
    assert "chapterRows ?" in _STUDY, "chapter section must be omitted when empty"
    assert "themeRows ?" in _STUDY, "theme section must be omitted when empty"


def test_every_scope_the_picker_sets_reaches_the_queue():
    for attr, param in (("data-chapter=", "params.set('chapter'"),
                        ("data-theme=", "params.set('theme'")):
        assert attr in _STUDY, f"{attr} button missing"
        assert param in _STUDY, f"{attr} is set but never forwarded to the queue"


def test_picker_is_reachable_from_the_subject_row():
    assert "renderPracticePicker(id)" in _STUDY


def test_tidy_bank_offers_both_backfills():
    assert "detect_chapters" in _STUDY and "group_themes" in _STUDY
