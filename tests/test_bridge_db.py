"""Tests for bridge_db read/write helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cost_tracker import bridge_db

COST_RECORDS_DDL = """
CREATE TABLE cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex', 'notion_os', 'personal_ops')),
    month TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(system, month)
);
"""


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Create a temp SQLite with the cost_records schema and sample rows."""
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(COST_RECORDS_DDL)
    conn.executemany(
        "INSERT INTO cost_records (system, month, amount, notes) VALUES (?, ?, ?, ?)",
        [
            ("cc", "2026-05", 120.0, "project:asc-radar May spend"),
            ("cc", "2026-04", 300.0, "project:asc-radar April spend"),
            ("codex", "2026-05", 45.0, None),
            ("notion_os", "2026-05", 10.0, "project:notion-os tooling"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


class TestCostTopProjects:
    def test_returns_sorted_by_spend(self, tmp_db):
        # tmp_db has only cost_records — triggers fallback path (system-level totals)
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db)

        assert len(result) >= 1
        # Highest spender first
        assert result[0]["total_usd"] >= result[-1]["total_usd"]

    def test_fallback_returns_system_key_not_project(self, tmp_db):
        # Without session_costs table the fallback emits 'system' rows, not 'project' rows
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db)

        assert all("system" in r for r in result)
        assert all("note" in r for r in result)
        assert all("project" not in r for r in result)

    def test_fallback_includes_cc_and_codex_systems(self, tmp_db):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db)

        systems = [r["system"] for r in result]
        assert "cc" in systems
        assert "codex" in systems

    def test_missing_db_returns_error(self, tmp_path):
        result = bridge_db.cost_top_projects(db_path=tmp_path / "nonexistent.db")

        assert len(result) == 1
        assert result[0]["error"] == "bridge_db_unavailable"

    def test_fallback_aggregates_cc_across_months(self, tmp_db):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db)

        # cc has April (300) + May (120) = 420 total
        cc_row = next((r for r in result if r.get("system") == "cc"), None)
        assert cc_row is not None
        assert cc_row["total_usd"] == pytest.approx(420.0)


class TestLatestCostRecord:
    def test_returns_existing_record(self, tmp_db):
        result = bridge_db.latest_cost_record(system="cc", month="2026-05", db_path=tmp_db)

        assert result["exists"] is True
        assert result["amount_usd"] == pytest.approx(120.0)
        assert result["recorded_at"]

    def test_missing_record_returns_exists_false(self, tmp_db):
        result = bridge_db.latest_cost_record(system="cc", month="2026-06", db_path=tmp_db)

        assert result == {"system": "cc", "month": "2026-06", "exists": False}

    def test_missing_db_returns_error(self, tmp_path):
        result = bridge_db.latest_cost_record(db_path=tmp_path / "nonexistent.db")

        assert result["error"] == "bridge_db_unavailable"

    def test_connect_failure_returns_bridge_db_error(self, tmp_db, monkeypatch):
        def fail_connect(*args, **kwargs):
            raise sqlite3.OperationalError("cannot open database")

        monkeypatch.setattr(bridge_db, "_connect", fail_connect)

        result = bridge_db.latest_cost_record(system="cc", month="2026-05", db_path=tmp_db)

        assert result["error"] == "bridge_db_error"
        assert result["detail"] == "cannot open database"


class TestInsertCostRecord:
    def test_insert_valid_record(self, tmp_db):
        # Use personal_ops which is allowed by the DB CHECK constraint
        result = bridge_db.insert_cost_record(
            month="2026-06",
            amount=99.99,
            system="personal_ops",
            notes="test insert",
            db_path=tmp_db,
        )
        assert result["status"] == "inserted"
        assert isinstance(result["record_id"], int)

    def test_round_trip_readable_after_insert(self, tmp_db):
        bridge_db.insert_cost_record(
            month="2026-06",
            amount=55.0,
            system="notion_os",
            notes="project:roundtrip-test",
            db_path=tmp_db,
        )
        # Fallback path returns 'system' rows when session_costs table is absent
        result = bridge_db.cost_top_projects(window_days=9999, db_path=tmp_db)
        # notion_os row should appear in fallback output
        systems = [r.get("system") for r in result]
        assert "notion_os" in systems

    def test_invalid_month_format_rejected(self, tmp_db):
        result = bridge_db.insert_cost_record(
            month="2026/05",
            amount=10.0,
            system="cc",
            db_path=tmp_db,
        )
        assert result["error"] == "validation_error"
        assert "YYYY-MM" in result["detail"]

    def test_invalid_system_rejected(self, tmp_db):
        result = bridge_db.insert_cost_record(
            month="2026-05",
            amount=10.0,
            system="unknown_system",
            db_path=tmp_db,
        )
        assert result["error"] == "validation_error"
        assert "system" in result["detail"]

    def test_negative_amount_rejected(self, tmp_db):
        result = bridge_db.insert_cost_record(
            month="2026-05",
            amount=-1.0,
            system="codex",
            db_path=tmp_db,
        )
        assert result["error"] == "validation_error"

    def test_missing_db_returns_error(self, tmp_path):
        result = bridge_db.insert_cost_record(
            month="2026-05",
            amount=10.0,
            system="cc",
            db_path=tmp_path / "nonexistent.db",
        )
        assert result["error"] == "bridge_db_unavailable"

    def test_duplicate_system_month_returns_integrity_error(self, tmp_db):
        # "cc" + "2026-05" already exists in tmp_db fixture
        result = bridge_db.insert_cost_record(
            month="2026-05",
            amount=200.0,
            system="cc",
            db_path=tmp_db,
        )
        assert result["error"] == "integrity_error"
