"""Tests for derived session classification and routing-violation queries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cost_tracker.classify import (
    classify_sessions,
    cost_routing_violations,
)

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

SESSION_CLASSIFICATION_DDL = """
CREATE TABLE session_classification (
    session_id     TEXT PRIMARY KEY REFERENCES session_costs(session_id),
    role           TEXT,
    task_class     TEXT,
    routing_basis  TEXT,
    dominant_model TEXT,
    confidence     REAL,
    method         TEXT NOT NULL,
    classified_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX idx_scl_routing ON session_classification(routing_basis);
"""


@pytest.fixture()
def routing_rules(tmp_path: Path) -> Path:
    rules = tmp_path / "auto-team.md"
    rules.write_text(
        """
        Implementation uses Sonnet by default.
        Research and review use Haiku.
        Opus is justified for architecture, auth, payments, and migrations.
        """,
        encoding="utf-8",
    )
    return rules


@pytest.fixture()
def bridge_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SESSION_COSTS_DDL + SESSION_CLASSIFICATION_DDL)
    conn.close()
    return db_path


def _write_transcript(projects_dir: Path, session_id: str, entries: list[dict]) -> None:
    session_dir = projects_dir / "encoded-project"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _insert_session(
    db_path: Path,
    session_id: str,
    *,
    started_at: str,
    cost: float,
    model_breakdown: dict[str, float],
    project_name: str = "fixture",
    source: str = "cc",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO session_costs (session_id, project_name, started_at, cost_usd, model_breakdown, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, project_name, started_at, cost, json.dumps(model_breakdown), source),
    )
    conn.commit()
    conn.close()


def _insert_classification(
    db_path: Path,
    session_id: str,
    *,
    routing_basis: str = "over-powered",
    confidence: float = 0.9,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO session_classification
            (session_id, role, task_class, routing_basis, dominant_model, confidence, method)
        VALUES (?, 'implementer', 'implementation', ?, 'claude-opus-4-8', ?, 'derived-v1')
        """,
        (session_id, routing_basis, confidence),
    )
    conn.commit()
    conn.close()


def _classification(db_path: Path, session_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM session_classification WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return row


def test_opus_implementation_classifies_over_powered(
    bridge_db: Path, tmp_path: Path, routing_rules: Path
) -> None:
    projects_dir = tmp_path / "projects"
    _insert_session(
        bridge_db,
        "sess-impl",
        started_at="2026-07-01T10:00:00Z",
        cost=10.0,
        model_breakdown={"claude-opus-4-8": 9.0, "claude-sonnet-5": 1.0},
    )
    _write_transcript(
        projects_dir,
        "sess-impl",
        [
            {"type": "user", "message": {"content": "Please implement the parser fix."}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write"}]}},
        ],
    )

    result = classify_sessions(
        db_path=bridge_db,
        projects_dir=projects_dir,
        routing_rules_path=routing_rules,
    )

    assert result["errors"] == []
    row = _classification(bridge_db, "sess-impl")
    assert row["task_class"] == "implementation"
    assert row["routing_basis"] == "over-powered"
    assert row["dominant_model"] == "claude-opus-4-8"
    assert row["confidence"] >= 0.6


def test_opus_architecture_classifies_justified(
    bridge_db: Path, tmp_path: Path, routing_rules: Path
) -> None:
    projects_dir = tmp_path / "projects"
    _insert_session(
        bridge_db,
        "sess-arch",
        started_at="2026-07-02T10:00:00Z",
        cost=12.0,
        model_breakdown={"claude-opus-4-8": 12.0},
    )
    _write_transcript(
        projects_dir,
        "sess-arch",
        [
            {
                "type": "user",
                "message": {
                    "content": "Design the migration architecture for auth and schema ownership."
                },
            },
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        ],
    )

    result = classify_sessions(
        db_path=bridge_db,
        projects_dir=projects_dir,
        routing_rules_path=routing_rules,
    )

    assert result["errors"] == []
    row = _classification(bridge_db, "sess-arch")
    assert row["task_class"] == "architecture"
    assert row["routing_basis"] == "justified"
    assert row["confidence"] >= 0.6


def test_low_signal_session_is_unknown_and_excluded_from_violations(
    bridge_db: Path, tmp_path: Path, routing_rules: Path
) -> None:
    projects_dir = tmp_path / "projects"
    _insert_session(
        bridge_db,
        "sess-low",
        started_at="2026-07-03T10:00:00Z",
        cost=99.0,
        model_breakdown={"claude-opus-4-8": 99.0},
    )
    _write_transcript(
        projects_dir,
        "sess-low",
        [{"type": "user", "message": {"content": "ok"}}],
    )

    result = classify_sessions(
        db_path=bridge_db,
        projects_dir=projects_dir,
        routing_rules_path=routing_rules,
    )

    assert result["errors"] == []
    row = _classification(bridge_db, "sess-low")
    assert row["task_class"] == "unknown"
    assert row["routing_basis"] == "unknown"
    assert row["confidence"] < 0.6

    violations = cost_routing_violations(db_path=bridge_db)
    assert violations["weeks"] == []
    assert violations["top_over_powered_buckets"] == []
    assert violations["confidence_distribution"]["confidence_gte_0_6"] == 0


def test_routing_violations_rank_buckets_and_surface_granularity(bridge_db: Path) -> None:
    _insert_session(
        bridge_db,
        "-Users-d",
        started_at="2026-06-10T10:00:00Z",
        cost=5841.0,
        model_breakdown={"claude-opus-4-8": 5841.0},
        project_name="home-adhoc",
        source="ccusage",
    )
    _insert_classification(bridge_db, "-Users-d", confidence=0.95)
    _insert_session(
        bridge_db,
        "123e4567-e89b-12d3-a456-426614174000",
        started_at="2026-07-01T10:00:00Z",
        cost=45.25,
        model_breakdown={"claude-opus-4-8": 45.25},
        project_name="personal-ops",
        source="ccusage",
    )
    _insert_classification(
        bridge_db,
        "123e4567-e89b-12d3-a456-426614174000",
        confidence=0.92,
    )
    _insert_session(
        bridge_db,
        "-Users-d-Projects-noisy-low-confidence",
        started_at="2026-07-02T10:00:00Z",
        cost=9999.0,
        model_breakdown={"claude-opus-4-8": 9999.0},
        project_name="noisy-low-confidence",
        source="ccusage",
    )
    _insert_classification(
        bridge_db,
        "-Users-d-Projects-noisy-low-confidence",
        confidence=0.59,
    )

    violations = cost_routing_violations(db_path=bridge_db)

    buckets = violations["top_over_powered_buckets"]
    assert [bucket["project_name"] for bucket in buckets] == ["home-adhoc", "personal-ops"]
    assert buckets[0]["rank"] == 1
    assert buckets[0]["wasted_usd_upper_bound"] == 5841.0
    assert buckets[0]["granularity"] == "bucket"
    assert buckets[0]["bucket_rows"] == 1
    assert buckets[0]["session_rows"] == 0
    assert buckets[1]["granularity"] == "session"
    assert buckets[1]["bucket_rows"] == 0
    assert buckets[1]["session_rows"] == 1
    assert "noisy-low-confidence" not in {bucket["project_name"] for bucket in buckets}
    assert "weekly figures as directional only" in violations["caveat"]
    assert "bucket ranking as the reliable view" in violations["caveat"]
