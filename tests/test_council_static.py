"""Static checks of the Council page's work-run controls (static/js/council.js)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "council.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    """The source of one top-level function, up to the next top-level declaration."""
    start = re.search(rf"^(?:async )?function {name}\(", _JS, re.MULTILINE)
    assert start, name
    end = re.search(r"^(?:async )?function |^const |^// -{10}", _JS[start.end():], re.MULTILINE)
    return _JS[start.start():start.end() + (end.start() if end else len(_JS))]


def test_the_work_toggle_shows_only_with_full_tools_and_a_workspace():
    available = _function("workAvailable")
    assert "S.tools === 'full'" in available and "S.workspace" in available
    seats = _function("renderSeats")
    assert "workAvailable()" in seats and "data-work-mode" in seats


def test_a_work_run_starts_with_its_own_call_and_fields():
    convene = _function("convene")
    assert "/work`" in convene
    for field in ("goal:", "rounds: S.rounds", "while_waiting: S.whileWaiting", "workspace: S.workspace",
                  "budget_confirmed"):
        assert field in convene, field


def test_the_rounds_field_takes_1_to_20():
    seats = _function("renderSeats")
    assert 'id="council-rounds"' in seats and 'type="number"' in seats
    assert 'min="1"' in seats and 'max="${MAX_ROUNDS}"' in seats and "const MAX_ROUNDS = 20;" in _JS
    assert "S.rounds = " in _function("onPaneChange")


def test_the_while_waiting_select_has_the_three_choices():
    seats = _function("renderSeats")
    assert 'id="council-while-waiting"' in seats and "While waiting for me" in seats
    for value in ("'stop'", "'continue'", "'vote'"):
        assert f"[{value}," in _JS, value
    assert "S.whileWaiting = " in _function("onPaneChange")


def test_rounds_and_while_waiting_are_remembered_with_the_session():
    config = _function("currentConfig")
    assert "rounds: S.rounds" in config and "while_waiting: S.whileWaiting" in config
    apply = _function("applyConfig")
    assert "cfg?.rounds" in apply and "cfg?.while_waiting" in apply


def test_each_round_has_a_badge_its_messages_and_each_opinion_its_diff_stat():
    turn = _function("renderTurn")
    assert "Round ${" in turn and "/${" in turn
    assert 'data-sec="messages"' in _function("messagesList") and "messagesList(" in turn
    assert "diffStat(" in _function("memberCard")


def test_the_end_of_a_run_says_why_and_how_to_fetch_the_branch():
    end = _function("workEnd")
    assert "STOP_REASONS" in end and "git fetch " in end and "w.branch" in end and "data-copy-text" in end


def test_live_rounds_follow_the_server_events():
    on_event = _function("onEvent")
    assert "ev.work" in on_event and "ev.question" in on_event


ATTACHMENT = ("Work on branch seat-1 since the round started at abc1234567 (git diff abc1234567 def4567890).\n\n"
              "Commits:\ndef4567 seat-1 round 1\n\n"
              "Files:\n parser.py | 12 ++++++------\n tests/test_parser.py | 4 ++++\n"
              " 2 files changed, 10 insertions(+), 6 deletions(-)\n\n"
              "Patch:\ndiff --git a/parser.py b/parser.py\n+# 9 files changed in a comment\n")


def test_the_diff_stat_is_read_from_the_file_list_of_the_attachment():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = _function("diffStat") + "\nconsole.log(JSON.stringify(" + json.dumps(
        [ATTACHMENT, "No committed changes on branch seat-2 since the round started at abc1234567.",
         "No committed changes on branch seat-2 since abc.\n\nUncommitted changes (in no commit, so they cannot "
         "be merged):\n?? notes.txt", ""]) + ".map(diffStat)));"
    result = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["2 files changed, 10 insertions(+), 6 deletions(-)", "no committed changes",
                                         "no committed changes · uncommitted changes left out", ""]


def test_the_connect_panel_has_a_github_row():
    connect = _function("renderConnect")
    assert "GitHub" in connect and "Connected as" in connect and "github_pat_" in connect
    assert 'data-act="github-token"' in connect and 'data-act="github-disconnect"' in connect
    assert "jget('/api/github/connection')" in _function("loadGitHub")
    save = _function("connectGitHub")
    assert "'/api/github/connection'" in save and "method: 'PUT'" in save and "token" in save
    assert "jdel('/api/github/connection')" in _function("disconnectGitHub")
    click = _function("onPaneClick")
    assert "'github-token'" in click and "'github-disconnect'" in click
    assert "loadGitHub()" in _function("init")


def test_the_pending_requests_feed_the_waiting_indicator():
    assert "jget('/api/council/requests?status=pending')" in _function("loadRequests")
    assert "Waiting for you" in _function("renderWaiting")
    assert 'id="council-waiting"' in _JS
    assert "loadRequests()" in _function("init")
    on_event = _function("onEvent")
    assert "case 'request':" in on_event and "renderWaiting()" in on_event


def test_a_round_shows_its_requests_with_approve_and_deny():
    cards = _function("requestsList")
    assert "data-req-approve" in cards and "data-req-deny" in cards and "head_sha" in cards and "branch" in cards
    assert "esc(r.body)" in cards and "md(" not in cards                 # agents' text stays plain text
    assert "requestsList(turn)" in _function("renderTurn")
    decide = _function("decideRequest")
    assert "jpost(`/api/council/requests/${encodeURIComponent(id)}/${verb}`" in decide
    click = _function("onPaneClick")
    assert "data-req-approve" in click and "'approve'" in click and "'deny'" in click
    assert "pr_url" in _function("requestOutcome")


def test_a_round_shows_what_was_pushed():
    assert "w.pushed" in _function("pushNote") and "w.push_error" in _function("pushNote")
    assert "pushNote(w)" in _function("renderTurn")


def test_no_emoji_were_added():
    assert not re.search("[\U0001F300-\U0001FAFF]", _JS)


def test_approving_takes_two_clicks_on_the_card_and_no_browser_dialog():
    # Embedded browsers and app webviews suppress confirm(): there the dialog
    # never shows, confirm() returns false, and Approve silently does nothing.
    assert "confirm(" not in _function("decideRequest")
    click = _function("onPaneClick")
    assert "dataset.armed" in click and "approveLabel(" in click
    label = _function("approveLabel")
    assert "Confirm: merge #" in label and "head_sha" in label and "Confirm: open the pull request" in label
