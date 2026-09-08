"""Extraction should leave a bank you can practise by chapter and theme.

Previously extraction saved questions and stopped; grouping was a separate
pass you had to find in Tidy bank on another screen, so the practice picker
sat on "Nothing grouped yet" until you went looking.

Extraction already runs a best-effort pass after saving (_link_deck_parts,
"an extraction must never fail because grouping did"). Chapter detection and
theme clustering join it under the same rule, and report what they did.

Opt-out is the ``study_auto_group`` preference: the passes cost model calls
and time, so anyone who wants extraction to stay fast can turn them off.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import routes.study.materials as materials


@pytest.fixture
def spy(monkeypatch):
    """Record the post-extraction passes without calling a model."""
    calls = []

    async def fake_link(user, deck_id, only_material=None):
        calls.append(("link_parts", deck_id, only_material))

    async def fake_chapters(user, material_id):
        calls.append(("chapters", material_id))
        return {"chapters": 3}

    async def fake_themes(user, deck_id):
        calls.append(("themes", deck_id))
        return {"themes": 5, "labelled": 40}

    monkeypatch.setattr(materials._common, "_link_deck_parts", fake_link)
    monkeypatch.setattr(materials, "run_detect_chapters", fake_chapters, raising=False)
    monkeypatch.setattr(materials, "run_cluster_themes", fake_themes, raising=False)
    return calls


def _run(spy_calls, *, created=4, pref="", **kw):
    """Drive only the post-extraction hook, with extraction itself stubbed."""
    import routes.study._common as common

    def read_pref(owner, key):
        return pref if key == "study_auto_group" else ""

    original = common._read_pref
    common._read_pref = read_pref
    try:
        return asyncio.run(materials._post_extraction_passes(
            "alice", "deck-1", "mat-1", created, **kw))
    finally:
        common._read_pref = original


def _kinds(calls):
    return [c[0] for c in calls]


# --------------------------------------------------------------------------
# The default: extraction leaves a grouped bank
# --------------------------------------------------------------------------

def test_extraction_groups_chapters_and_themes(spy):
    report = _run(spy)
    assert _kinds(spy) == ["link_parts", "chapters", "themes"]
    assert report["chapters"] == {"chapters": 3}
    assert report["themes"] == {"themes": 5, "labelled": 40}


def test_chapters_are_scoped_to_the_material_just_extracted(spy):
    _run(spy)
    assert ("chapters", "mat-1") in spy


def test_themes_are_clustered_across_the_whole_subject(spy):
    """A theme spans documents, so it is a deck-level pass."""
    _run(spy)
    assert ("themes", "deck-1") in spy


def test_nothing_runs_when_no_questions_were_created(spy):
    """A no-op extraction must not spend model calls on grouping."""
    report = _run(spy, created=0)
    assert spy == []
    assert report["skipped"] == "nothing extracted"


# --------------------------------------------------------------------------
# Opt-out
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pref", ["0", "false", "off", "no"])
def test_the_preference_turns_grouping_off(spy, pref):
    report = _run(spy, pref=pref)
    assert _kinds(spy) == ["link_parts"], "grouping ran despite the opt-out"
    assert report["skipped"] == "study_auto_group is off"


@pytest.mark.parametrize("pref", ["", "1", "true", "on"])
def test_grouping_is_on_by_default_and_for_truthy_values(spy, pref):
    _run(spy, pref=pref)
    assert "themes" in _kinds(spy)


# --------------------------------------------------------------------------
# It must never break an extraction that succeeded
# --------------------------------------------------------------------------

def test_a_failing_chapter_pass_does_not_stop_themes(spy, monkeypatch):
    async def boom(user, material_id):
        raise RuntimeError("model timed out")

    monkeypatch.setattr(materials, "run_detect_chapters", boom, raising=False)
    report = _run(spy)

    assert "themes" in _kinds(spy), "one failed pass cancelled the other"
    assert "model timed out" in str(report["chapters"])


def test_a_failing_theme_pass_is_reported_not_raised(spy, monkeypatch):
    async def boom(user, deck_id):
        raise RuntimeError("no model configured")

    monkeypatch.setattr(materials, "run_cluster_themes", boom, raising=False)
    report = _run(spy)

    assert "no model configured" in str(report["themes"])


def test_a_failing_link_pass_does_not_stop_grouping(spy, monkeypatch):
    async def boom(user, deck_id, only_material=None):
        raise RuntimeError("link failed")

    monkeypatch.setattr(materials._common, "_link_deck_parts", boom)
    report = _run(spy)

    assert _kinds(spy) == ["chapters", "themes"]
    assert report["chapters"] == {"chapters": 3}


def test_the_hook_never_raises(spy, monkeypatch):
    """Whatever happens, the caller's extraction result must survive."""
    async def boom(*a, **k):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(materials._common, "_link_deck_parts", boom)
    monkeypatch.setattr(materials, "run_detect_chapters", boom, raising=False)
    monkeypatch.setattr(materials, "run_cluster_themes", boom, raising=False)

    report = _run(spy)   # must not raise
    assert isinstance(report, dict)
