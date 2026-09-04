"""Static regressions for the Odysseus Omnigent surface."""

from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_APP = (_REPO / "static" / "app.js").read_text(encoding="utf-8")
_SETTINGS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
# Upstream moved UI_VIS_MAP out of app.js into its own module.
_UI_VIS = (_REPO / "static" / "js" / "ui_visibility.js").read_text(encoding="utf-8")


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
    assert "'tool-omnigent':" in _UI_VIS


def test_integrations_picker_includes_omnigent_agent_not_paid_glm_duplicate():
    assert "omnigent:" in _SETTINGS
    assert "Omnigent Agent" in _SETTINGS
    assert "/api/omnigent/bundle.tar.gz" in _SETTINGS
    assert "glm-coding-plan" not in _SETTINGS


def test_omnigent_module_is_a_launcher():
    module = (_REPO / "static" / "js" / "omnigent.js").read_text(encoding="utf-8")

    # pure launcher: boot Omnigent + open its own chat UI, no config window
    assert "launchCrew" in module
    assert 'data-omnigent-action="launch"' in module
    assert "/api/omnigent/launch" in module
    assert "Launch Omnigent" in module
    assert "Open Omnigent" in module
    # the agent-config window + goal-gated path are gone
    assert "renderOrchestrator" not in module
    assert "renderAgents" not in module
    assert "omnigent-goal-input" not in module
    assert "/api/omnigent/runs" not in module


def test_app_shell_binds_omnigent_button_once():
    app = (_REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert app.count("const toolOmnigentBtn = el('tool-omnigent-btn');") == 1


def test_app_shell_opens_omnigent_directly():
    app = (_REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert "omnigentModule.open();" in app
    assert "Modals.toggle('omnigent-modal')" not in app
