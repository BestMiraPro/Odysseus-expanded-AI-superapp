"""Schema upgrades must reach the current shape on every supported dialect.

Almost every column migration in ``core/database.py`` inspects the schema with
``PRAGMA table_info(...)`` — SQLite-only — inside a ``try/except`` that logs a
warning and moves on. On PostgreSQL the PRAGMA raises, the guard swallows it,
and the column is never added: the upgrade silently no-ops and the running
application queries columns that do not exist.

``Base.metadata`` is the authoritative description of the current schema, so
reconciling the live schema against it is dialect-agnostic by construction.
These tests drive that reconciliation over an "old" schema that is missing
columns and indexes, and assert it lands on the current shape.

PostgreSQL coverage runs when ``TEST_POSTGRES_URL`` points at a scratch
database, e.g.::

    docker run -d --rm -e POSTGRES_PASSWORD=pw -p 5433:5432 postgres:16
    TEST_POSTGRES_URL=postgresql+psycopg2://postgres:pw@127.0.0.1:5433/postgres

Without it the PostgreSQL cases skip loudly rather than passing vacuously.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from core import database as cdb
from core.database import Base


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")

# Representative of the migrations the review named: a plain nullable column,
# an indexed column, and a column carrying a default.
DROPPED = [
    ("documents", "archived"),
    ("documents", "tidy_verdict"),
    ("sessions", "owner"),
    ("study_reviews", "idempotency_key"),
]


def _make_engine(url, tmp_path):
    if url == "sqlite":
        return create_engine(f"sqlite:///{tmp_path / 'parity.db'}", poolclass=NullPool)
    return create_engine(url, poolclass=NullPool)


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path):
    if request.param == "postgresql":
        if not POSTGRES_URL:
            pytest.skip("TEST_POSTGRES_URL is not set; PostgreSQL parity not exercised")
        eng = _make_engine(POSTGRES_URL, tmp_path)
        Base.metadata.drop_all(bind=eng)
    else:
        eng = _make_engine("sqlite", tmp_path)
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        if request.param == "postgresql":
            Base.metadata.drop_all(bind=eng)
        eng.dispose()


def _degrade(engine, targets):
    """Turn the current schema into an older one by removing columns."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column in targets:
            for idx in inspector.get_indexes(table):
                if column in (idx.get("column_names") or []):
                    conn.execute(text(f'DROP INDEX {_idx_name(engine, table, idx["name"])}'))
            conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))


def _idx_name(engine, table, name):
    # SQLite indexes are database-scoped; PostgreSQL's live in the schema.
    return name if engine.dialect.name != "mysql" else f"{name} ON {table}"


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_reconciliation_restores_every_missing_column(engine):
    _degrade(engine, DROPPED)
    for table, column in DROPPED:
        assert column not in _columns(engine, table), "fixture did not degrade the schema"

    cdb.reconcile_schema_with_models(engine)

    for table, column in DROPPED:
        assert column in _columns(engine, table), (
            f"{table}.{column} was not restored on {engine.dialect.name}"
        )


def test_reconciliation_reports_what_it_changed(engine):
    _degrade(engine, DROPPED)
    applied = cdb.reconcile_schema_with_models(engine)
    for table, column in DROPPED:
        assert f"{table}.{column}" in applied


def test_reconciliation_is_idempotent(engine):
    _degrade(engine, DROPPED)
    first = cdb.reconcile_schema_with_models(engine)
    second = cdb.reconcile_schema_with_models(engine)
    assert first, "first pass should have applied changes"
    assert second == [], f"second pass re-applied {second}"


def test_full_schema_matches_the_models_after_reconciliation(engine):
    """Every model column exists, for every table — not just the sampled ones."""
    _degrade(engine, DROPPED)
    cdb.reconcile_schema_with_models(engine)

    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    missing = []
    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue
        live = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name not in live:
                missing.append(f"{table.name}.{col.name}")
    assert missing == [], f"columns still missing on {engine.dialect.name}: {missing}"


def test_indexes_declared_on_models_are_restored(engine):
    """A dropped index must come back, not just the column under it."""
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_sessions_owner"))
    before = {i["name"] for i in inspect(engine).get_indexes("sessions")}
    assert "ix_sessions_owner" not in before

    cdb.reconcile_schema_with_models(engine)

    after = {i["name"] for i in inspect(engine).get_indexes("sessions")}
    assert "ix_sessions_owner" in after


def test_unknown_tables_are_left_alone(engine):
    """A table outside the models (an old rename, another app) is not touched."""
    with engine.begin() as conn:
        # Not a model table, so the fixture's drop_all will not clear it.
        conn.execute(text("DROP TABLE IF EXISTS legacy_scratch"))
        conn.execute(text("CREATE TABLE legacy_scratch (id VARCHAR)"))
    try:
        cdb.reconcile_schema_with_models(engine)

        assert "legacy_scratch" in inspect(engine).get_table_names()
        assert _columns(engine, "legacy_scratch") == {"id"}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS legacy_scratch"))


def test_existing_data_survives_reconciliation(engine):
    """Adding a column must not rewrite or drop rows."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO documents (id, title, current_content, created_at, updated_at) "
            "VALUES ('d1', 'keep-me', 'body text', :ts, :ts)"
        ), {"ts": datetime(2026, 1, 1)})
    _degrade(engine, [("documents", "archived")])

    cdb.reconcile_schema_with_models(engine)

    with engine.connect() as conn:
        rows = list(conn.execute(text("SELECT id, title, current_content FROM documents")))
    assert rows == [("d1", "keep-me", "body text")]


def test_init_db_runs_the_reconciliation():
    """The mechanism is only worth anything if startup actually invokes it."""
    import inspect as _inspect

    source = _inspect.getsource(cdb.init_db)
    assert "reconcile_schema_with_models(engine)" in source
    # It must run before the hand-written migrations, so their backfills see
    # the columns on dialects where their own PRAGMA guard adds nothing.
    assert source.index("reconcile_schema_with_models") < source.index("_migrate_add_owner_column")
