"""Study's model must be choosable from Settings, where model config lives.

Every other tier — default chat, utility, teacher, research, vision — has a
card under Settings → Services. Study did not, even though its three settings
keys (``study_endpoint_id``, ``study_model``, ``study_text_model``) have always
been accepted by the settings API. The only picker was inside the Study panel's
header disclosure, which is not where anyone looks for model configuration.

These are structural checks: the card exists, its controls are wired to an
initialiser that runs, and it writes the keys the backend already reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_SETTINGS_JS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")

_CONTROLS = ["set-studyEpSelect", "set-studyModelSelect", "set-studyTextModelSelect"]


@pytest.mark.parametrize("control_id", _CONTROLS)
def test_the_card_has_its_controls(control_id):
    assert f'id="{control_id}"' in _INDEX, f"{control_id} is missing from Settings"


@pytest.mark.parametrize("control_id", _CONTROLS)
def test_each_control_appears_exactly_once(control_id):
    """A duplicated id makes getElementById pick one and silently orphan the other."""
    assert _INDEX.count(f'id="{control_id}"') == 1


def test_the_card_is_labelled_study():
    assert re.search(r">Study Model</h2>", _INDEX), (
        "the card has no heading naming it as Study's model"
    )


def test_the_initialiser_is_actually_called():
    """A defined-but-uncalled init is an inert card — the failure mode here."""
    assert "async function initStudyModel(" in _SETTINGS_JS
    assert re.search(r"^\s*initStudyModel\(\);", _SETTINGS_JS, re.MULTILINE), (
        "initStudyModel is defined but never invoked, so the card stays empty"
    )


@pytest.mark.parametrize("key", ["study_endpoint_id", "study_model", "study_text_model"])
def test_it_writes_the_keys_the_backend_reads(key):
    """These are the keys _resolve_study_model and _study_text_model read."""
    body = re.search(r"async function initStudyModel\(.*?\n\}", _SETTINGS_JS, re.DOTALL)
    assert body, "initStudyModel not found"
    assert key in body.group(0), f"the card never persists {key}"


@pytest.mark.parametrize("key", ["study_endpoint_id", "study_model", "study_text_model"])
def test_the_keys_are_accepted_by_the_settings_api(key):
    """POST /api/auth/settings only copies keys present in DEFAULT_SETTINGS —
    anything else is dropped silently, so the card would report a save that
    never happened."""
    from src.settings import DEFAULT_SETTINGS

    assert key in DEFAULT_SETTINGS, f"{key} would be discarded on save"


def test_changing_the_endpoint_clears_both_model_ids():
    """Both ids name models on the endpoint being replaced."""
    body = re.search(r"async function initStudyModel\(.*?\n\}", _SETTINGS_JS, re.DOTALL)
    assert body
    match = re.search(r"epSel\.addEventListener\('change',\s*function\(\)\s*\{([^}]*)\}",
                      body.group(0))
    assert match, "no change handler on the endpoint select"
    assert "refreshModels('', '')" in match.group(1), (
        "a stale model id survives an endpoint change and would dispatch to a "
        "model the new endpoint does not have"
    )
