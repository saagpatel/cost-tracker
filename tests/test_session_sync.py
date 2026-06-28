"""Tests for session_sync module and updated cost_top_projects."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cost_tracker import bridge_db
from cost_tracker.session_sync import _decode_project_name, sync_session_costs

# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------

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

# Canonical session_costs schema, owned and created by bridge-db (db.py:114).
# cost-tracker no longer defines this DDL; fixtures mirror bridge-db's schema so
# the sync is tested against exactly what it relies on in production.
SESSION_COSTS_DDL = """
CREATE TABLE session_costs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL UNIQUE,
    project_name    TEXT,
    started_at      TEXT    NOT NULL,
    cost_usd        REAL    NOT NULL,
    model_breakdown TEXT,
    source          TEXT    NOT NULL DEFAULT 'cc',
    recorded_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX idx_sc_project ON session_costs(project_name);
CREATE INDEX idx_sc_started ON session_costs(started_at DESC);
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db_cost_records_only(tmp_path: Path) -> Path:
    """Temp DB with only cost_records table (no session_costs)."""
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(COST_RECORDS_DDL)
    conn.executemany(
        "INSERT INTO cost_records (system, month, amount, notes) VALUES (?, ?, ?, ?)",
        [
            ("cc", "2026-05", 120.0, "project:asc-radar May spend"),
            ("cc", "2026-04", 300.0, "project:asc-radar April spend"),
            ("codex", "2026-05", 45.0, None),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def tmp_db_with_session_costs(tmp_path: Path) -> Path:
    """Temp DB with session_costs populated."""
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(COST_RECORDS_DDL + SESSION_COSTS_DDL)
    conn.executemany(
        """INSERT INTO session_costs
           (session_id, project_name, started_at, cost_usd, model_breakdown, source)
           VALUES (?, ?, ?, ?, '{}', 'cc')""",
        [
            ("aaa-111", "Afterimage", "2026-06-15T10:00:00.000Z", 5.50),
            ("aaa-222", "Afterimage", "2026-06-16T10:00:00.000Z", 3.25),
            ("bbb-111", "cost-tracker", "2026-06-17T10:00:00.000Z", 1.00),
            ("ccc-111", None, "2026-06-18T10:00:00.000Z", 0.75),  # unmapped
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def tmp_db_for_sync(tmp_path: Path) -> Path:
    """Empty temp DB with both tables for sync tests."""
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(COST_RECORDS_DDL + SESSION_COSTS_DDL)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests: _decode_project_name
# ---------------------------------------------------------------------------


class TestDecodeProjectName:
    def test_afterimage(self):
        assert _decode_project_name("-Users-d-Projects-Afterimage") == "Afterimage"

    def test_bridge_db(self):
        # Anchor-based decoding preserves the full project name including dashes
        assert _decode_project_name("-Users-d-Projects-bridge-db") == "bridge-db"

    def test_personal_ops_with_local_share(self):
        # '--' in dir name encodes '/.', e.g. ~/.local → '--local'
        # Anchor '--local-share-' captures the service name 'personal-ops' intact
        assert _decode_project_name("-Users-d--local-share-personal-ops") == "personal-ops"

    def test_root_user_skipped(self):
        # Bare home dir has no project-name component after username
        assert _decode_project_name("-Users-d") is None

    def test_private_tmp_skipped(self):
        # /private/tmp is a system path; skip on '-private-' prefix
        assert _decode_project_name("-private-tmp") is None

    def test_bare_claude_config_skipped(self):
        # ~/.claude itself is not a project
        assert _decode_project_name("-Users-d--claude") is None

    def test_empty_returns_none(self):
        assert _decode_project_name("") is None

    def test_only_dashes_returns_none(self):
        assert _decode_project_name("---") is None


# ---------------------------------------------------------------------------
# Tests: cost_top_projects fallback (no session_costs table)
# ---------------------------------------------------------------------------


class TestCostTopProjectsFallback:
    def test_fallback_when_no_session_costs_table(self, tmp_db_cost_records_only):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db_cost_records_only)

        assert len(result) >= 1
        # Fallback returns 'system' key, not 'project'
        assert all("system" in r for r in result)
        assert all("note" in r for r in result)
        assert "session_costs" in result[0]["note"].lower() or "sync" in result[0]["note"].lower()

    def test_fallback_no_project_key(self, tmp_db_cost_records_only):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db_cost_records_only)
        # Fallback rows must NOT have 'project' key (they have 'system')
        assert all("project" not in r for r in result)

    def test_missing_db_returns_error(self, tmp_path):
        result = bridge_db.cost_top_projects(db_path=tmp_path / "nonexistent.db")
        assert result[0]["error"] == "bridge_db_unavailable"


# ---------------------------------------------------------------------------
# Tests: cost_top_projects with session_costs data
# ---------------------------------------------------------------------------


class TestCostTopProjectsWithData:
    def test_returns_project_key(self, tmp_db_with_session_costs):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db_with_session_costs)
        # At least some rows should have 'project' key
        project_rows = [r for r in result if "project" in r]
        assert len(project_rows) >= 1

    def test_aggregates_by_project(self, tmp_db_with_session_costs):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db_with_session_costs)
        afterimage = next((r for r in result if r.get("project") == "Afterimage"), None)
        assert afterimage is not None
        assert afterimage["total_usd"] == pytest.approx(5.50 + 3.25)
        assert afterimage["session_count"] == 2

    def test_sorted_by_spend_descending(self, tmp_db_with_session_costs):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db_with_session_costs)
        totals = [r["total_usd"] for r in result]
        assert totals == sorted(totals, reverse=True)

    def test_includes_unmapped_sessions(self, tmp_db_with_session_costs):
        result = bridge_db.cost_top_projects(window_days=90, db_path=tmp_db_with_session_costs)
        unmapped = next((r for r in result if r.get("project") == "(unmapped)"), None)
        assert unmapped is not None
        assert unmapped["total_usd"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Tests: sync_session_costs
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str,
    cost: float,
    project_hint: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> dict:
    return {
        "period": session_id,
        "metadata": {"lastActivity": "2026-06-19T10:00:00.000Z"},
        "totalCost": cost,
        "modelBreakdowns": [
            {
                "modelName": model,
                "cost": cost,
                "inputTokens": 1000,
                "outputTokens": 500,
            }
        ],
    }


class TestSyncSessionCosts:
    def test_syncs_sessions_into_db(self, tmp_db_for_sync, tmp_path):
        sessions = [
            _make_session("sess-aaa", 2.50),
            _make_session("sess-bbb", 1.00),
        ]
        result = sync_session_costs(
            db_path=tmp_db_for_sync,
            ccusage_fn=lambda: sessions,
        )

        assert result["synced"] == 2
        assert result["skipped"] == 0
        assert result["errors"] == []

        # Verify rows in DB
        conn = sqlite3.connect(str(tmp_db_for_sync))
        rows = conn.execute(
            "SELECT session_id, cost_usd FROM session_costs ORDER BY cost_usd DESC"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][1] == pytest.approx(2.50)

    def test_idempotent_upsert(self, tmp_db_for_sync):
        sessions = [_make_session("sess-aaa", 2.50)]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        # Update cost and sync again
        updated = [_make_session("sess-aaa", 3.00)]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: updated)

        assert result["synced"] == 1
        conn = sqlite3.connect(str(tmp_db_for_sync))
        row = conn.execute(
            "SELECT cost_usd FROM session_costs WHERE session_id = 'sess-aaa'"
        ).fetchone()
        conn.close()
        assert row[0] == pytest.approx(3.00)

    def test_ccusage_failure_returns_error(self, tmp_db_for_sync):
        result = sync_session_costs(
            db_path=tmp_db_for_sync,
            ccusage_fn=lambda: None,
        )
        assert result["synced"] == 0
        assert len(result["errors"]) > 0

    def test_missing_db_returns_error(self, tmp_path):
        result = sync_session_costs(
            db_path=tmp_path / "nonexistent.db",
            ccusage_fn=lambda: [_make_session("sess-aaa", 1.0)],
        )
        assert result["synced"] == 0
        assert any("not found" in e for e in result["errors"])

    def test_model_breakdown_stored_as_json(self, tmp_db_for_sync):
        sessions = [_make_session("sess-model", 1.5, model="claude-opus-4-8")]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        conn = sqlite3.connect(str(tmp_db_for_sync))
        row = conn.execute(
            "SELECT model_breakdown FROM session_costs WHERE session_id = 'sess-model'"
        ).fetchone()
        conn.close()

        breakdown = json.loads(row[0])
        assert "claude-opus-4-8" in breakdown
        assert breakdown["claude-opus-4-8"] == pytest.approx(1.5)

    def test_skips_sessions_without_id(self, tmp_db_for_sync):
        sessions = [
            {"totalCost": 1.0, "metadata": {}, "modelBreakdowns": []},  # no period/sessionId
            _make_session("sess-valid", 2.0),
        ]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        assert result["synced"] == 1
        assert result["skipped"] == 1

    def test_does_not_create_session_costs_when_missing(self, tmp_db_cost_records_only):
        """bridge-db owns the session_costs schema; cost-tracker must not create it.

        Given a bridge.db with only cost_records (no session_costs), sync returns a
        clean error and leaves the schema untouched — it does NOT bootstrap the table.
        """
        sessions = [_make_session("sess-aaa", 2.50)]
        result = sync_session_costs(
            db_path=tmp_db_cost_records_only,
            ccusage_fn=lambda: sessions,
        )

        assert result["synced"] == 0
        assert any("session_costs" in e for e in result["errors"])

        conn = sqlite3.connect(str(tmp_db_cost_records_only))
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_costs'"
        ).fetchone()
        conn.close()
        assert exists is None, "cost-tracker must not create the session_costs table"

    def test_syncs_against_bridge_db_canonical_schema(self, tmp_db_for_sync):
        """The upsert relies on bridge-db's canonical schema (session_id UNIQUE, id PK)."""
        sessions = [_make_session("sess-canon", 4.00)]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        assert result["synced"] == 1
        conn = sqlite3.connect(str(tmp_db_for_sync))
        row = conn.execute(
            "SELECT id, cost_usd FROM session_costs WHERE session_id = 'sess-canon'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == pytest.approx(4.00)
        # The canonical schema assigns an integer autoincrement id on insert; the synced
        # row carries one, proving it landed in bridge-db's canonical table.
        assert isinstance(row[0], int)


class TestSyncMalformedBreakdowns:
    """A malformed modelBreakdowns value must not crash the whole sync."""

    def test_tolerates_null_model_breakdowns(self, tmp_db_for_sync):
        session = {
            "period": "sess-null",
            "metadata": {},
            "totalCost": 1.0,
            "modelBreakdowns": None,
        }
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: [session])
        assert result["synced"] == 1
        assert result["errors"] == []

    def test_tolerates_non_dict_breakdown_entries(self, tmp_db_for_sync):
        session = {
            "period": "sess-bad",
            "metadata": {},
            "totalCost": 1.0,
            "modelBreakdowns": ["garbage", {"modelName": "claude-sonnet-4-6", "cost": 1.0}],
        }
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: [session])
        assert result["synced"] == 1
        conn = sqlite3.connect(str(tmp_db_for_sync))
        row = conn.execute(
            "SELECT model_breakdown FROM session_costs WHERE session_id = 'sess-bad'"
        ).fetchone()
        conn.close()
        breakdown = json.loads(row[0])
        assert breakdown["claude-sonnet-4-6"] == pytest.approx(1.0)
        assert "garbage" not in breakdown
