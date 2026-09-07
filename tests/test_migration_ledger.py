"""D01 — semantic backfills need versions, a failure policy, and a bounded gap.

Reconciliation makes every model *column* appear on any dialect. It does not
run the migrations that derive values, and 10 of those still open a raw
sqlite3 connection, so on PostgreSQL they no-op silently. A column existing
there is not evidence its data was migrated.

Three things are pinned here:

* a ledger, so a semantic backfill records that it ran, on any dialect;
* an actionable failure policy — a *required* backfill that fails must raise
  rather than let a partially-upgraded database look healthy;
* an explicit inventory of the SQLite-only backfills that remain, so the gap
  is bounded and visible instead of growing quietly.

PostgreSQL cases run when ``TEST_POSTGRES_URL`` is set; without it they skip
loudly rather than passing vacuously.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from core import database as cdb


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path):
    if request.param == "postgresql":
        if not POSTGRES_URL:
            pytest.skip("TEST_POSTGRES_URL is not set; PostgreSQL not exercised")
        eng = create_engine(POSTGRES_URL, poolclass=NullPool)
        with eng.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {cdb.MIGRATION_LEDGER_TABLE}"))
            conn.execute(text("DROP TABLE IF EXISTS ledger_probe"))
    else:
        eng = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", poolclass=NullPool)
    try:
        yield eng
    finally:
        if request.param == "postgresql":
            with eng.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {cdb.MIGRATION_LEDGER_TABLE}"))
                conn.execute(text("DROP TABLE IF EXISTS ledger_probe"))
        eng.dispose()


def _probe_table(engine):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE ledger_probe (id VARCHAR, val VARCHAR)"))
        conn.execute(text("INSERT INTO ledger_probe (id, val) VALUES ('a', NULL)"))


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------

def test_a_backfill_runs_and_is_recorded(engine):
    _probe_table(engine)
    ran = []

    def fill(conn):
        ran.append(1)
        conn.execute(text("UPDATE ledger_probe SET val = 'filled' WHERE val IS NULL"))

    assert cdb.run_versioned_backfill(engine, "probe-1", fill) == "applied"
    assert ran == [1]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT val FROM ledger_probe")).scalar() == "filled"
    assert cdb.migration_applied(engine, "probe-1") is True


def test_a_recorded_backfill_does_not_run_again(engine):
    _probe_table(engine)
    ran = []

    def fill(conn):
        ran.append(1)

    assert cdb.run_versioned_backfill(engine, "probe-1", fill) == "applied"
    assert cdb.run_versioned_backfill(engine, "probe-1", fill) == "skipped"
    assert ran == [1], "the backfill ran twice despite being recorded"


def test_an_unrelated_backfill_is_independent(engine):
    _probe_table(engine)
    cdb.run_versioned_backfill(engine, "probe-1", lambda c: None)
    assert cdb.migration_applied(engine, "probe-2") is False
    assert cdb.run_versioned_backfill(engine, "probe-2", lambda c: None) == "applied"


def test_a_failed_backfill_is_not_recorded_as_applied(engine):
    """It must be retried on the next startup, not silently marked done."""
    _probe_table(engine)

    def boom(conn):
        raise RuntimeError("disk went away")

    assert cdb.run_versioned_backfill(engine, "probe-1", boom) == "failed"
    assert cdb.migration_applied(engine, "probe-1") is False


def test_a_required_backfill_raises_instead_of_limping_on(engine):
    """The actionable failure policy: a broken required invariant must stop
    startup rather than leave a half-upgraded database looking healthy."""
    _probe_table(engine)

    def boom(conn):
        raise RuntimeError("disk went away")

    with pytest.raises(RuntimeError):
        cdb.run_versioned_backfill(engine, "probe-1", boom, required=True)
    assert cdb.migration_applied(engine, "probe-1") is False


def test_a_failed_backfill_does_not_leave_partial_writes(engine):
    """It runs in a transaction: either the whole derivation lands or none."""
    _probe_table(engine)

    def half(conn):
        conn.execute(text("UPDATE ledger_probe SET val = 'partial'"))
        raise RuntimeError("failed after writing")

    assert cdb.run_versioned_backfill(engine, "probe-1", half) == "failed"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT val FROM ledger_probe")).scalar() is None


def test_the_ledger_table_is_created_on_demand(engine):
    inspector = inspect(engine)
    assert cdb.MIGRATION_LEDGER_TABLE not in inspector.get_table_names()
    cdb.run_versioned_backfill(engine, "probe-1", lambda c: None)
    assert cdb.MIGRATION_LEDGER_TABLE in inspect(engine).get_table_names()


# --------------------------------------------------------------------------
# The remaining gap, bounded
# --------------------------------------------------------------------------

# Migrations that still transform data through a raw sqlite3 connection and
# therefore no-op on PostgreSQL. Shrinking this list is the rest of D01;
# nothing may be added to it without a deliberate decision.
KNOWN_SQLITE_ONLY_BACKFILLS = {
    "_migrate_add_last_message_at_column",
    "_migrate_add_study_summary_columns",
    "_migrate_study_attempt_confidence_to_numeric",
    "_migrate_add_api_token_scopes_column",
    "_migrate_assign_legacy_owner",
    "_migrate_backfill_document_owner_from_session",
    "_migrate_add_task_automation_columns",
    "_migrate_backfill_task_folders",
    # SQLite-only by design: FTS5 virtual tables have no PostgreSQL equivalent
    # (PostgreSQL would use tsvector). Not a portability gap.
    "_migrate_chat_messages_fts",
}


def _sqlite_only_data_backfills():
    import ast
    import inspect as _inspect
    import re

    source = _inspect.getsource(cdb)
    tree = ast.parse(source)
    lines = source.splitlines()
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_migrate"):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        sqlite_only = (
            "sqlite3.connect" in body
            or "_sqlite_db_path" in body
            or "DATABASE_URL.replace" in body
            or "PRAGMA" in body
        )
        if sqlite_only and re.search(r"\b(UPDATE|INSERT|DELETE)\b", body, re.I):
            found.add(node.name)
    return found


def test_no_new_sqlite_only_backfill_appears():
    found = _sqlite_only_data_backfills()
    added = found - KNOWN_SQLITE_ONLY_BACKFILLS
    assert added == set(), (
        "these migrations transform data through a raw sqlite3 connection and "
        f"will no-op on PostgreSQL: {sorted(added)}. Either make them "
        "dialect-aware via run_versioned_backfill, or add them to "
        "KNOWN_SQLITE_ONLY_BACKFILLS with a reason."
    )


def test_the_known_gap_list_stays_honest():
    """A migration that has been made portable must leave the list."""
    found = _sqlite_only_data_backfills()
    stale = KNOWN_SQLITE_ONLY_BACKFILLS - found
    assert stale == set(), (
        f"these are no longer SQLite-only; remove them from the list: {sorted(stale)}"
    )
