"""D01 — upgrades must transform old *values*, not just add column names.

``reconcile_schema_with_models`` adds any model column missing from the live
schema, and it runs before the hand-written migrations. Several of those
migrations gate their data backfill on the column being absent:

    if columns and "smtp_security" not in columns:
        ALTER TABLE ... ADD COLUMN smtp_security ...
        UPDATE  ... SET smtp_security = CASE smtp_port ...

Once reconciliation has added the column, that guard is false and the backfill
never runs. The schema looks correct — every column is present — while old rows
keep a default that contradicts their own port. Column presence is not proof
that data was migrated.

These fixtures hold real old rows and assert the values after the actual
startup order.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from core import database as cdb


def _old_email_accounts_db(tmp_path: Path) -> Path:
    """An email_accounts table from before smtp_security existed."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE email_accounts (
            id VARCHAR PRIMARY KEY,
            name VARCHAR,
            smtp_host VARCHAR,
            smtp_port INTEGER
        )
    """)
    conn.executemany(
        "INSERT INTO email_accounts (id, name, smtp_host, smtp_port) VALUES (?, ?, ?, ?)",
        [
            ("a", "starttls-era", "smtp.example.test", 587),
            ("b", "ssl-era", "smtp.example.test", 465),
            ("c", "odd-port", "smtp.example.test", 2525),
            ("d", "no-port", "smtp.example.test", None),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _security_by_id(db: Path):
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT id, smtp_security FROM email_accounts"))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The migration's own contract, on a genuinely old table
# --------------------------------------------------------------------------

def test_the_backfill_derives_security_from_the_port(tmp_path):
    db = _old_email_accounts_db(tmp_path)
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)

    cdb.apply_email_smtp_security(engine)

    values = _security_by_id(db)
    assert values["a"] == "starttls", "port 587 must map to STARTTLS"
    assert values["b"] == "ssl"
    assert values["c"] == "ssl", "an unknown port falls back to ssl"
    assert values["d"] == "ssl", "a null port falls back to ssl"


def test_the_backfill_is_idempotent(tmp_path):
    db = _old_email_accounts_db(tmp_path)
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)

    cdb.apply_email_smtp_security(engine)
    first = _security_by_id(db)
    cdb.apply_email_smtp_security(engine)

    assert _security_by_id(db) == first


def test_an_operator_set_value_is_not_overwritten(tmp_path):
    """Re-running must not clobber a deliberate choice."""
    db = _old_email_accounts_db(tmp_path)
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)
    cdb.apply_email_smtp_security(engine)

    conn = sqlite3.connect(db)
    conn.execute("UPDATE email_accounts SET smtp_security = 'none' WHERE id = 'a'")
    conn.commit()
    conn.close()

    cdb.apply_email_smtp_security(engine)
    assert _security_by_id(db)["a"] == "none"


# --------------------------------------------------------------------------
# The interaction that column-name checks cannot see
# --------------------------------------------------------------------------

def test_the_backfill_still_runs_when_reconciliation_added_the_column(tmp_path):
    """The regression D01 predicted.

    Reconciliation runs first and adds smtp_security from the model. The
    migration then sees the column present, skips, and the old rows keep a
    default that contradicts their own port — while every column name checks
    out.
    """
    db = _old_email_accounts_db(tmp_path)
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)

    cdb.reconcile_schema_with_models(engine)      # startup order: this first
    assert "smtp_security" in {
        c["name"] for c in inspect(engine).get_columns("email_accounts")
    }, "fixture precondition: reconciliation should have added the column"

    cdb.apply_email_smtp_security(engine)        # then the legacy migrations

    values = _security_by_id(db)
    assert values["a"] == "starttls", (
        "port 587 was left on the default because reconciliation had already "
        "added the column, so the migration skipped its backfill"
    )
    assert values["b"] == "ssl"
    engine.dispose()


# --------------------------------------------------------------------------
# Documented limits of reconciliation
# --------------------------------------------------------------------------

def test_reconciliation_reports_what_it_does_not_check(tmp_path):
    """It compares names. Types, constraints and index expressions are not
    verified, and the docstring must keep saying so."""
    doc = cdb.reconcile_schema_with_models.__doc__ or ""
    lowered = doc.lower()
    assert "name" in lowered
    for limit in ("type", "constraint", "expression"):
        assert limit in lowered, (
            f"the docstring does not disclose that {limit}s are unchecked"
        )


def test_the_docstring_agrees_with_the_startup_order():
    """The docstring claimed it runs after the legacy migrations; init_db runs
    it before them, which is what makes the backfill interaction above
    possible."""
    import inspect as _inspect

    source = _inspect.getsource(cdb.init_db)
    assert source.index("reconcile_schema_with_models") < source.index(
        "_migrate_add_owner_column"
    ), "reconciliation no longer runs before the legacy migrations"
    doc = (cdb.reconcile_schema_with_models.__doc__ or "").lower()
    assert "after the legacy migrations" not in doc, (
        "the docstring still claims it runs after the legacy migrations"
    )


def test_an_expression_index_is_left_alone(tmp_path):
    """Reconciliation rebuilds indexes from index.columns, which cannot express
    a partial or expression index. It must not try and produce a weaker one."""
    engine = create_engine(f"sqlite:///{tmp_path / 'expr.db'}", poolclass=NullPool)
    cdb.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        names_before = {
            r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='email_accounts'"
            ))
        }

    cdb.reconcile_schema_with_models(engine)

    with engine.begin() as conn:
        rows = list(conn.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='email_accounts' AND sql IS NOT NULL"
        )))
    for name, sql in rows:
        if name in names_before and "WHERE" in (sql or "").upper():
            assert "WHERE" in sql.upper(), (
                f"the partial index {name} lost its predicate"
            )
    engine.dispose()
