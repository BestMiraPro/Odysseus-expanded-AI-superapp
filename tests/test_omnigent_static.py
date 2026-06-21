"""Static regressions for the Odysseus Omnigent surface."""

from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_APP = (_REPO / "static" / "app.js").read_text(encoding="utf-8")
_SETTINGS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")


def test_omnigent_has_sidebar_rail_modal_and_script_hooks():
    assert 'id="rail-omnigent"' in _INDEX
    assert 'id="tool-omnigent-btn"' in _INDEX
    assert 'id="omnigent-modal"' in _INDEX
    assert 'src="/static/js/omnigent.js"' in _INDEX
    assert "'rail-omnigent': 'tool-omnigent-btn'" in _APP
    assert "'/omnigent':" in _APP
    assert "omnigentModule.open" in _APP


def test_omnigent_visibility_can_be_managed_with_other_tools():
    assert "data-ui-key=\"tool-omnigent\"" in _INDEX
    assert "'tool-omnigent':" in _APP


def test_integrations_picker_includes_omnigent_agent_not_paid_glm_duplicate():
    assert "omnigent:" in _SETTINGS
    assert "Omnigent Agent" in _SETTINGS
    assert "/api/omnigent/bundle.tar.gz" in _SETTINGS
    assert "glm-coding-plan" not in _SETTINGS


def test_omnigent_module_mentions_free_glm_website_as_unsupported_note():
    module = (_REPO / "static" / "js" / "omnigent.js").read_text(encoding="utf-8")

    assert "glm-free-web" in module
    assert "free website" in module.lower()
    assert "not an API connector" in module


def test_omnigent_module_prioritizes_easy_setup_and_worker_roster():
    module = (_REPO / "static" / "js" / "omnigent.js").read_text(encoding="utf-8")

    assert "Start Claude + Codex" in module
    assert "Easy setup" in module
    assert "/api/omnigent/workers" in module
    assert "Worker roster" in module
    assert "Advanced bridge" in module
