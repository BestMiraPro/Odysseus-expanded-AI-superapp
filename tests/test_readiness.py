"""Tests for the readiness / integrity self-check (src/readiness.py)."""

import json
import logging

from src.readiness import check_readiness

LEAK = "private-marker /srv/private/auth.json postgresql://u:secret-marker@db/app"


def assert_no_internal_details(serialized: str) -> None:
    for marker in ("private-marker", "/srv/private/auth.json", "secret-marker"):
        assert marker not in serialized


def test_readiness_reports_core_subsystems():
    result = check_readiness()

    assert {"ready", "version", "checks", "timestamp"}.issubset(result.keys())
    checks = result["checks"]
    for name in ("database", "data_dir", "local_first"):
        assert name in checks, f"missing check: {name}"

    # In the dev/test environment the local SQLite DB and data dir are present,
    # so the critical checks must pass and overall readiness must be True.
    assert checks["database"]["ok"] is True, checks["database"]
    assert checks["data_dir"]["ok"] is True, checks["data_dir"]
    assert result["ready"] is True, result


def test_local_first_check_is_informational_never_fatal():
    result = check_readiness()
    lf = result["checks"]["local_first"]
    # local_first reports whether storage stays on-host but must never gate
    # readiness — a remote database is a valid deployment.
    assert lf["ok"] is True
    assert "local" in lf


def test_database_failure_returns_stable_label(monkeypatch, caplog):
    """A failing DB probe must report a fixed label, never the exception."""
    import core.database as cdb

    class _BoomEngine:
        def connect(self):
            raise RuntimeError(LEAK)

    monkeypatch.setattr(cdb, "engine", _BoomEngine())

    with caplog.at_level(logging.WARNING):
        result = check_readiness()

    assert result["ready"] is False
    db_check = result["checks"]["database"]
    assert db_check["ok"] is False
    assert db_check["error"] == "database check failed"
    assert_no_internal_details(json.dumps(result))
    assert_no_internal_details(caplog.text)
    assert "error_type=RuntimeError" in caplog.text


def test_data_dir_failure_returns_stable_label(monkeypatch, caplog):
    """A failing data-dir probe must report a fixed label, never the exception."""
    import src.readiness as readiness

    def boom(*args, **kwargs):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(readiness.os, "makedirs", boom)

    with caplog.at_level(logging.WARNING):
        result = check_readiness()

    assert result["ready"] is False
    data_check = result["checks"]["data_dir"]
    assert data_check["ok"] is False
    assert data_check["error"] == "data directory check failed"
    assert_no_internal_details(json.dumps(result))
    assert_no_internal_details(caplog.text)
    assert "error_type=RuntimeError" in caplog.text
