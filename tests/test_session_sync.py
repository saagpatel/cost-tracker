"""Tests for session_sync module and updated cost_top_projects."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from cost_tracker import bridge_db
from cost_tracker.session_sync import (
    _build_session_project_map,
    _decode_project_name,
    sync_session_costs,
)

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

    def test_workflow_hex_tail_maps_to_home_adhoc(self):
        assert _decode_project_name("wf_7d0e9cc5-085") == "home-adhoc"

    def test_personal_ops_with_local_share(self):
        # '--' in dir name encodes '/.', e.g. ~/.local → '--local'
        # Anchor '--local-share-' captures the service name 'personal-ops' intact
        assert _decode_project_name("-Users-d--local-share-personal-ops") == "personal-ops"

    def test_projects_anchor_recovers_dash_mangled_name(self):
        assert _decode_project_name("-Users-d-Projects-Devil-s-Advocate") == "Devil's Advocate"

    def test_projects_anchor_recovers_dash_mangled_moved_repo(self):
        assert _decode_project_name("-Users-d-Projects-Fun-GamePrjs-BattleGrid") == "BattleGrid"

    def test_unknown_no_anchor_home_fragment_maps_to_home_adhoc(self):
        assert _decode_project_name("-Users-d-085") == "home-adhoc"

    def test_root_user_maps_to_home_adhoc(self):
        # Bare home dir has no project-name component, but it is still a named bucket.
        assert _decode_project_name("-Users-d") == "home-adhoc"

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

    def test_home_project_dir_maps_uuid_sessions_to_home_adhoc(self, tmp_path):
        projects_dir = tmp_path / "projects"
        home_dir = projects_dir / "-Users-d"
        home_dir.mkdir(parents=True)
        (home_dir / "sess-home.jsonl").write_text("{}\n")

        mapping = _build_session_project_map(projects_dir)

        assert mapping["sess-home"] == "home-adhoc"


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

    def test_sync_maps_bare_home_session_id_to_named_bucket(self, tmp_db_for_sync):
        session = {
            "sessionId": "-Users-d",
            "lastActivity": "2026-06-19T10:00:00.000Z",
            "totalCost": 7.25,
            "modelBreakdowns": [],
        }
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: [session])

        assert result["synced"] == 1
        conn = sqlite3.connect(str(tmp_db_for_sync))
        row = conn.execute(
            "SELECT project_name FROM session_costs WHERE session_id = '-Users-d'"
        ).fetchone()
        conn.close()
        assert row[0] == "home-adhoc"


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


# ---------------------------------------------------------------------------
# Tests: regression coverage for the 2026-07-26 cost-attribution defects
#
# Each class below pins one bug that made per-project attribution report a small
# fraction of actual spend while the sync still returned zero errors. Every test
# here fails on the prior code.
# ---------------------------------------------------------------------------


def _make_live_session(
    session_id: str,
    cost: float,
    last_activity: str,
    project_path: str = "Unknown Project",
    model: str = "claude-opus-5",
) -> dict:
    """Build an entry in the installed-ccusage shape (`sessionId` + `projectPath`)."""
    return {
        "sessionId": session_id,
        "projectPath": project_path,
        "lastActivity": last_activity,
        "totalCost": cost,
        "modelBreakdowns": [{"modelName": model, "cost": cost}],
    }


def _fetch(db_path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class TestStartedAtRefreshesOnResync:
    """`started_at` must track ccusage's lastActivity, not freeze at first sync.

    The upsert previously refreshed cost_usd but not started_at, so most rows
    stayed pinned to their first-sync date while their running total kept
    climbing — dropping them out of every rolling-window query.
    """

    def test_started_at_follows_last_activity(self, tmp_db_for_sync):
        first = [_make_live_session("-Users-d", 100.0, "2026-06-19")]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: first)

        later = [_make_live_session("-Users-d", 10_676.95, "2026-07-25")]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: later)

        rows = _fetch(
            tmp_db_for_sync,
            "SELECT started_at, cost_usd FROM session_costs WHERE session_id = '-Users-d'",
        )
        assert rows[0][0] == "2026-07-25", "started_at froze at the first-sync date"
        assert rows[0][1] == pytest.approx(10_676.95)

    def test_refreshed_row_stays_inside_the_window(self, tmp_db_for_sync):
        """The end-to-end symptom: a stale stamp silently hides live spend."""
        stale = [_make_live_session("uuid-active", 500.0, "2026-01-01")]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: stale)

        fresh_date = date.today().isoformat()
        fresh = [_make_live_session("uuid-active", 500.0, fresh_date)]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: fresh)

        result = bridge_db.cost_top_projects(window_days=14, db_path=tmp_db_for_sync)
        assert any(r.get("total_usd") == pytest.approx(500.0) for r in result)


class TestSessionKeyCollision:
    """`sessionId` is not unique across ccusage rows; the key must disambiguate.

    Live output carries hundreds of entries all keyed `subagents`, differing only
    by `projectPath`. Against a UNIQUE session_id column they upserted onto one
    row, so only the last-written entry's spend survived.
    """

    def test_subagent_rows_do_not_overwrite_each_other(self, tmp_db_for_sync):
        sessions = [
            _make_live_session("subagents", 203.42, "2026-07-18", "-Users-d/parent-a"),
            _make_live_session("subagents", 193.07, "2026-07-19", "-Users-d/parent-b"),
            _make_live_session("subagents", 85.39, "2026-07-12", "-Users-d/parent-c"),
        ]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        assert result["synced"] == 3
        assert result["key_collisions"] == 0
        total = _fetch(tmp_db_for_sync, "SELECT ROUND(SUM(cost_usd), 2) FROM session_costs")[0][0]
        assert total == pytest.approx(203.42 + 193.07 + 85.39)

    def test_workflow_rows_sharing_a_project_path_stay_distinct(self, tmp_db_for_sync):
        """`wf_*` rows repeat a projectPath, so the key needs both components."""
        shared = "-Users-d-Projects/parent-x/subagents/workflows"
        sessions = [
            _make_live_session("wf_aaa", 1.50, "2026-07-20", shared),
            _make_live_session("wf_bbb", 2.50, "2026-07-20", shared),
        ]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        assert result["synced"] == 2
        total = _fetch(tmp_db_for_sync, "SELECT SUM(cost_usd) FROM session_costs")[0][0]
        assert total == pytest.approx(4.0)

    def test_plain_buckets_keep_their_bare_session_id(self, tmp_db_for_sync):
        """Existing live rows must not duplicate under the new key scheme."""
        sessions = [_make_live_session("-Users-d-Projects-evals", 5.0, "2026-07-20")]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        ids = [r[0] for r in _fetch(tmp_db_for_sync, "SELECT session_id FROM session_costs")]
        assert ids == ["-Users-d-Projects-evals"]

    def test_subagent_rows_attribute_to_the_parent_project(self, tmp_db_for_sync):
        """Subagent spend belongs to its parent's project, not to nothing."""
        sessions = [
            _make_live_session("subagents", 42.0, "2026-07-18", "-Users-d-Projects-evals/parent-a")
        ]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        rows = _fetch(tmp_db_for_sync, "SELECT project_name FROM session_costs")
        assert rows[0][0] == "evals"


class TestSupersededRowReconciliation:
    """Rekeying leaves the legacy bare-id rows behind; they must not double-count.

    The composite key rewrites `subagents` and `wf_*` spend under new ids. Left
    alone, the old rows survive as duplicates of spend already re-recorded.
    """

    def test_legacy_bare_row_is_removed_after_rekey(self, tmp_db_for_sync):
        legacy = [_make_live_session("subagents", 4.23, "2026-07-01")]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: legacy)
        assert _fetch(tmp_db_for_sync, "SELECT COUNT(*) FROM session_costs")[0][0] == 1

        rekeyed = [
            _make_live_session("subagents", 203.42, "2026-07-18", "-Users-d/parent-a"),
            _make_live_session("subagents", 193.07, "2026-07-19", "-Users-d/parent-b"),
        ]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: rekeyed)

        assert result["superseded_removed"] == 1
        ids = sorted(r[0] for r in _fetch(tmp_db_for_sync, "SELECT session_id FROM session_costs"))
        assert ids == ["-Users-d/parent-a/subagents", "-Users-d/parent-b/subagents"]
        total = _fetch(tmp_db_for_sync, "SELECT SUM(cost_usd) FROM session_costs")[0][0]
        assert total == pytest.approx(203.42 + 193.07), "legacy row double-counted"

    def test_history_ccusage_stopped_reporting_is_preserved(self, tmp_db_for_sync):
        """A row merely absent from ccusage is history, not a duplicate.

        Without this restriction one truncated ccusage response would delete real
        cost records.
        """
        first = [
            _make_live_session("-Users-d-Projects-old", 99.0, "2026-05-01"),
            _make_live_session("subagents", 1.0, "2026-05-02", "-Users-d/parent-a"),
        ]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: first)

        later = [_make_live_session("subagents", 2.0, "2026-07-02", "-Users-d/parent-a")]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: later)

        assert result["superseded_removed"] == 0
        ids = sorted(r[0] for r in _fetch(tmp_db_for_sync, "SELECT session_id FROM session_costs"))
        assert "-Users-d-Projects-old" in ids, "unreported history must survive"

    def test_bucket_still_reported_under_its_bare_id_is_kept(self, tmp_db_for_sync):
        """A bare id this run still writes is not superseded."""
        sessions = [
            _make_live_session("-Users-d", 500.0, "2026-07-20"),
            _make_live_session("subagents", 5.0, "2026-07-20", "-Users-d/parent-a"),
        ]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        ids = sorted(r[0] for r in _fetch(tmp_db_for_sync, "SELECT session_id FROM session_costs"))
        assert ids == ["-Users-d", "-Users-d/parent-a/subagents"]


class TestSyncCounterHonesty:
    """`synced` must count rows written, not insert attempts.

    Counting attempts is what let the collision hide behind "489 synced, 0 errors".
    """

    def test_reports_input_entries_and_collisions(self, tmp_db_for_sync):
        collided = "-Users-d/parent-a"
        sessions = [
            _make_live_session("subagents", 10.0, "2026-07-18", collided),
            _make_live_session("subagents", 20.0, "2026-07-19", collided),
        ]
        result = sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        assert result["input_entries"] == 2
        assert result["synced"] == 1, "two entries collapsed onto one key"
        assert result["key_collisions"] == 1, "the collapse must be reported, not hidden"


class TestLifetimeBucketGranularity:
    """Directory buckets hold all-time spend and must stay out of windowed views.

    Windowing a lifetime total is bimodal: a dominant home-directory bucket either
    vanished from a 14-day window or landed in a 60-day one whole. Neither is
    window spend.
    """

    def test_bucket_excluded_from_window_even_when_recent(self, tmp_db_for_sync):
        today = date.today().isoformat()
        sessions = [
            _make_live_session("-Users-d", 10_676.95, today),
            _make_live_session("uuid-real", 12.0, today, "-Users-d/parent-a"),
        ]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        result = bridge_db.cost_top_projects(window_days=14, db_path=tmp_db_for_sync)
        windowed = [r for r in result if "total_usd" in r]
        assert sum(r["total_usd"] for r in windowed) == pytest.approx(12.0)

    def test_excluded_spend_is_reported_not_silently_dropped(self, tmp_db_for_sync):
        today = date.today().isoformat()
        sessions = [
            _make_live_session("-Users-d", 10_676.95, today),
            _make_live_session("uuid-real", 12.0, today, "-Users-d/parent-a"),
        ]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        result = bridge_db.cost_top_projects(window_days=14, db_path=tmp_db_for_sync)
        advisory = next((r for r in result if r.get("granularity") == "lifetime_excluded"), None)
        assert advisory is not None, "held-back spend must be surfaced"
        assert advisory["excluded_usd"] == pytest.approx(10_676.95)
        assert advisory["bucket_count"] == 1

    def test_lifetime_buckets_reports_all_time_spend(self, tmp_db_for_sync):
        sessions = [
            _make_live_session("-Users-d", 10_676.95, "2026-06-19"),
            _make_live_session("-Users-d-Projects-evals", 257.09, "2026-07-01"),
            _make_live_session("uuid-real", 12.0, "2026-07-20", "-Users-d/parent-a"),
        ]
        sync_session_costs(db_path=tmp_db_for_sync, ccusage_fn=lambda: sessions)

        buckets = bridge_db.cost_lifetime_buckets(db_path=tmp_db_for_sync)
        by_project = {b["project"]: b for b in buckets}
        assert by_project["home-adhoc"]["total_usd"] == pytest.approx(10_676.95)
        assert by_project["evals"]["total_usd"] == pytest.approx(257.09)
        assert all(b["granularity"] == "lifetime" for b in buckets)
        assert "uuid-real" not in by_project, "session rows are not lifetime buckets"

    def test_lifetime_buckets_missing_db_returns_error(self, tmp_path):
        result = bridge_db.cost_lifetime_buckets(db_path=tmp_path / "nonexistent.db")
        assert result[0]["error"] == "bridge_db_unavailable"
