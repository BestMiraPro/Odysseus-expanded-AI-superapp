"""Database startup must choose driver options from the URL dialect."""

from pathlib import Path
import runpy

import pytest
import sqlalchemy


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgresql://user:sqlite_password@localhost/app", {}),
        ("postgresql://user:password@localhost/sqlite_archive", {}),
        ("sqlite:///:memory:", {"check_same_thread": False}),
        ("sqlite+pysqlite:///:memory:", {"check_same_thread": False}),
    ],
)
def test_database_startup_uses_dialect_for_connection_options(monkeypatch, tmp_path, url, expected):
    import src.constants

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    options = {}

    class EngineBoundaryReached(Exception):
        pass

    def capture_engine(database_url, **kwargs):
        options.update(kwargs)
        # Stop before schema setup: this test needs neither a live database nor
        # replacement ORM models, and must leave the imported app DB untouched.
        raise EngineBoundaryReached

    monkeypatch.setattr(sqlalchemy, "create_engine", capture_engine)
    with pytest.raises(EngineBoundaryReached):
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "core" / "database.py"))
    assert options["connect_args"] == expected
