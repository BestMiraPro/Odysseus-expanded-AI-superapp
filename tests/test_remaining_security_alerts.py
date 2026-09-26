"""Regression checks for the exception sources still reported on dev."""

import asyncio
import json
from types import SimpleNamespace

LEAK = "SECRET-SENTINEL /private/server/config"


def fail(*args, **kwargs):
    raise ValueError(LEAK)


def test_pdf_failure_does_not_become_document_content(monkeypatch):
    import pypdf
    from src.document_processor import _process_pdf

    monkeypatch.setattr(pypdf, "PdfReader", fail)
    assert LEAK not in _process_pdf("synthetic.pdf")


def test_mail_auth_failure_keeps_provider_guidance_without_raw_error():
    from routes.email_helpers import _friendly_email_auth_error

    assert LEAK not in _friendly_email_auth_error("IMAP", "mail.example", ValueError(LEAK))
    assert "Microsoft" in _friendly_email_auth_error("SMTP", "smtp.office365.com", "5.7.139")


def test_omnigent_session_transport_error_is_private(monkeypatch):
    from src.omnigent_manager import OmnigentManager

    monkeypatch.setattr(OmnigentManager, "status", lambda self: {"running": True, "url": "http://localhost:8000"})
    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = OmnigentManager().sessions()
    assert result["sessions"] == [] and result["running"]
    assert LEAK not in json.dumps(result)


def test_unsupported_pty_does_not_return_import_error(monkeypatch):
    from routes import shell_routes as shell

    monkeypatch.setattr(shell, "PTY_SUPPORTED", False)
    monkeypatch.setattr(shell, "_PTY_IMPORT_ERROR", ImportError(LEAK))

    async def collect():
        return [chunk async for chunk in shell._generate_pty("echo hi", 10, None)]

    chunks = asyncio.run(collect())
    assert "pty_unsupported" in "".join(chunks)
    assert LEAK not in "".join(chunks)


def test_teacher_resolution_error_is_private(monkeypatch, caplog):
    from src import teacher_escalation as teacher

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: {"teacher_enabled": True, "teacher_model": "synthetic"}.get(key, default))
    monkeypatch.setattr(teacher, "evaluate_turn_regex", lambda *args: ("failure", "test"))
    monkeypatch.setattr("src.ai_interaction._resolve_model", fail)

    async def collect():
        return [chunk async for chunk in teacher.run_teacher_inline(
            student_endpoint_url="http://localhost:8000", student_messages=[],
            student_tool_events=[], student_reply="")]

    chunks = asyncio.run(collect())
    assert "escalation_failed" in "".join(chunks)
    assert LEAK not in "".join(chunks) + caplog.text


def test_carddav_import_validation_error_is_private(monkeypatch, caplog):
    from routes.contacts import contacts_routes as contacts

    monkeypatch.setattr(contacts, "_get_carddav_config", lambda: {"url": "https://dav.example"})
    monkeypatch.setattr(contacts, "_carddav_base_url", fail)
    result = contacts._import_vcards("BEGIN:VCARD\nEND:VCARD")
    assert result["imported"] == 0
    assert LEAK not in json.dumps(result) + caplog.text


def endpoint(router, path):
    return next(route.endpoint for route in router.routes if route.path == path)


def test_caldav_probe_validation_error_is_private(monkeypatch):
    from routes import calendar_routes as calendar

    monkeypatch.setattr(calendar, "_require_user", lambda request: "synthetic")
    monkeypatch.setattr("src.caldav_sync.validate_caldav_url", fail)

    async def body():
        return {"url": "https://dav.example", "username": "test", "password": "synthetic"}

    result = asyncio.run(endpoint(calendar.setup_calendar_routes(), "/api/calendar/test")(
        SimpleNamespace(json=body)))
    assert result["ok"] is False
    assert LEAK not in json.dumps(result)


def test_send_config_failure_is_private(monkeypatch):
    from fastapi import BackgroundTasks
    from routes import email_routes as mail

    monkeypatch.setattr(mail, "_resolve_send_config", fail)
    result = asyncio.run(endpoint(mail.setup_email_routes(), "/api/email/send")(
        mail.SendEmailRequest(to="test@example.com", subject="synthetic", body="test"),
        BackgroundTasks(), owner="synthetic"))
    assert result["success"] is False
    assert LEAK not in json.dumps(result)


def test_unsubscribe_transport_value_error_is_private(monkeypatch):
    from routes import email_routes as mail

    monkeypatch.setattr(mail, "_imap", fail)
    result = endpoint(mail.setup_email_routes(), "/api/email/unsubscribe/execute")(
        {"uid": "1"}, owner="synthetic")
    assert result["success"] is False
    assert LEAK not in json.dumps(result)


def test_background_shell_launch_error_is_private(monkeypatch, tmp_path):
    from routes import shell_routes as shell

    monkeypatch.setattr(shell, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(shell, "find_bash", lambda: None)
    monkeypatch.setattr(shell.subprocess, "Popen", fail)

    async def collect():
        return [chunk async for chunk in shell._generate_win_detached("echo test", None)]

    chunks = asyncio.run(collect())
    assert '"exit_code": -1' in "".join(chunks)
    assert LEAK not in "".join(chunks)
