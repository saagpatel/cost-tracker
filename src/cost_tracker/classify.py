"""Heuristic session classification for bridge-db session cost rows."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cost_tracker.bridge_db import _LIFETIME_BUCKET_SQL

BRIDGE_DB_PATH = Path.home() / ".local" / "share" / "bridge-db" / "bridge.db"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
ROUTING_RULES_PATH = Path.home() / ".claude" / "rules" / "auto-team.md"
METHOD = "derived-v1"
MIN_QUERY_CONFIDENCE = 0.6

_READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
_WRITE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_SUBAGENT_TOOLS = {"Task", "Agent"}
_TOOL_KEYS = {"name", "toolName", "tool_name", "tool"}

_ARCHITECTURE_KEYWORDS = {
    "architecture",
    "architectural",
    "schema",
    "migration",
    "migrations",
    "auth",
    "authentication",
    "authorization",
    "payment",
    "payments",
    "security",
}
_REVIEW_KEYWORDS = {
    "review",
    "reviewer",
    "audit",
    "validator",
    "code-reviewer",
    "python-reviewer",
    "security review",
}
_HYGIENE_KEYWORDS = {
    "cleanup",
    "clean up",
    "sweep",
    "dependency",
    "dependencies",
    "bump",
    "config",
    "format",
    "lint",
    "ruff",
    "pyright",
}
_IMPLEMENTATION_KEYWORDS = {
    "implement",
    "fix",
    "patch",
    "add",
    "build",
    "refactor",
    "test",
}

_EXPECTED_TIER = {
    "architecture": 3,
    "implementation": 2,
    "hygiene": 2,
    "research": 1,
    "review": 1,
    "conversation": 1,
}


@dataclass
class TranscriptSignals:
    found: bool = False
    line_count: int = 0
    text_fragments: list[str] = field(default_factory=list)
    tool_counts: Counter[str] = field(default_factory=Counter)
    is_sidechain: bool = False

    @property
    def combined_text(self) -> str:
        return "\n".join(self.text_fragments).lower()

    @property
    def read_count(self) -> int:
        return sum(self.tool_counts[name] for name in _READ_TOOLS)

    @property
    def write_count(self) -> int:
        return sum(self.tool_counts[name] for name in _WRITE_TOOLS)

    @property
    def subagent_spawn_count(self) -> int:
        return sum(self.tool_counts[name] for name in _SUBAGENT_TOOLS)


def _connect_rw(path: Path) -> sqlite3.Connection:
    # mode=rw, not rwc: bridge-db owns creation of the DB file and schema.
    uri = f"file:{path}?mode=rw"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _routing_policy_available(path: Path) -> bool:
    # Read-only oracle check. The derived-v1 policy below mirrors this file; the
    # file remains operator-owned and is never edited by cost-tracker.
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _model_tier(model_name: str | None) -> int | None:
    if not model_name:
        return None
    lower = model_name.lower()
    if "opus" in lower or "fable" in lower:
        return 3
    if "sonnet" in lower:
        return 2
    if "haiku" in lower:
        return 1
    return None


def _dominant_model(model_breakdown: str | None) -> str | None:
    if not model_breakdown:
        return None
    try:
        parsed = json.loads(model_breakdown)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    best_model: str | None = None
    best_cost = 0.0
    for model, cost in parsed.items():
        if not isinstance(model, str):
            continue
        try:
            numeric_cost = float(cost)
        except (TypeError, ValueError):
            continue
        if numeric_cost > best_cost:
            best_model = model
            best_cost = numeric_cost
    return best_model


def _extract_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, dict):
        for child in value.values():
            _extract_strings(child, out)
        return
    if isinstance(value, list):
        for child in value:
            _extract_strings(child, out)


def _collect_signal_from_json(value: Any, signals: TranscriptSignals) -> None:
    if isinstance(value, dict):
        if value.get("isSidechain") is True or value.get("sidechain") is True:
            signals.is_sidechain = True
        for key, child in value.items():
            if key in _TOOL_KEYS and isinstance(child, str):
                normalized = child.strip()
                if normalized:
                    signals.tool_counts[normalized] += 1
            _collect_signal_from_json(child, signals)
        return
    if isinstance(value, list):
        for child in value:
            _collect_signal_from_json(child, signals)


def _entry_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    entry_type = value.get("type")
    if entry_type in {"user", "assistant"}:
        return value.get("message")
    if entry_type == "queue-operation":
        return value.get("content")
    return None


def _transcript_paths(session_id: str, projects_dir: Path) -> list[Path]:
    direct = list(projects_dir.glob(f"*/{session_id}.jsonl")) if projects_dir.is_dir() else []
    if direct:
        return direct

    project_dir = projects_dir / session_id
    if project_dir.is_dir():
        return sorted(project_dir.glob("*.jsonl"))
    return []


def read_transcript_signals(
    session_id: str, projects_dir: Path = CLAUDE_PROJECTS_DIR
) -> TranscriptSignals:
    signals = TranscriptSignals()
    for path in _transcript_paths(session_id, projects_dir):
        signals.found = True
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    signals.line_count += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        signals.text_fragments.append(line)
                        continue
                    if isinstance(obj, dict) and (
                        obj.get("isSidechain") is True or obj.get("sidechain") is True
                    ):
                        signals.is_sidechain = True
                    payload = _entry_payload(obj)
                    if payload is None:
                        continue
                    _collect_signal_from_json(payload, signals)
                    strings: list[str] = []
                    _extract_strings(payload, strings)
                    signals.text_fragments.extend(strings)
        except OSError:
            continue
    return signals


def _has_any(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _keyword_hits(text: str, keywords: set[str]) -> int:
    return sum(text.count(keyword) for keyword in keywords)


def _classify_role(signals: TranscriptSignals) -> str:
    if signals.is_sidechain:
        return "subagent"
    if signals.subagent_spawn_count > 0:
        return "lead-coordinator"
    if signals.found:
        return "solo"
    return "unknown"


def _classify_task(signals: TranscriptSignals) -> str:
    if not signals.found:
        return "unknown"
    text = signals.combined_text
    if signals.line_count <= 2 and not signals.tool_counts and len(text.strip()) < 120:
        return "unknown"

    architecture_hits = _keyword_hits(text, _ARCHITECTURE_KEYWORDS)
    implementation_hits = _keyword_hits(text, _IMPLEMENTATION_KEYWORDS) + signals.write_count
    review_hits = _keyword_hits(text, _REVIEW_KEYWORDS)
    hygiene_hits = _keyword_hits(text, _HYGIENE_KEYWORDS)

    if review_hits > 0 and signals.write_count == 0:
        return "review"
    if architecture_hits >= 2 and architecture_hits >= implementation_hits:
        return "architecture"
    if hygiene_hits > 0 and hygiene_hits >= implementation_hits:
        return "hygiene"
    if implementation_hits > 0:
        return "implementation"
    if signals.read_count > 0:
        return "research"
    if architecture_hits > 0:
        return "architecture"
    if review_hits > 0:
        return "review"
    if text.strip():
        return "conversation"
    return "unknown"


def _routing_basis(task_class: str, dominant_model: str | None) -> str:
    expected = _EXPECTED_TIER.get(task_class)
    actual = _model_tier(dominant_model)
    if expected is None or actual is None:
        return "unknown"
    if actual > expected:
        return "over-powered"
    if actual < expected:
        return "under-powered"
    return "justified"


def _confidence(
    *,
    signals: TranscriptSignals,
    task_class: str,
    role: str,
    dominant_model: str | None,
    routing_policy_available: bool,
) -> float:
    score = 0.0
    if dominant_model is not None:
        score += 0.2
    if _model_tier(dominant_model) is not None:
        score += 0.1
    if signals.found:
        score += 0.2
    if task_class != "unknown":
        score += 0.25
    if signals.tool_counts or _has_any(
        signals.combined_text, _ARCHITECTURE_KEYWORDS | _REVIEW_KEYWORDS
    ):
        score += 0.15
    if role != "unknown":
        score += 0.05
    if routing_policy_available:
        score += 0.05
    if task_class == "unknown":
        score = min(score, MIN_QUERY_CONFIDENCE - 0.05)
    return round(min(score, 1.0), 3)


def classify_session_row(
    row: sqlite3.Row | dict[str, Any],
    *,
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    routing_rules_path: Path = ROUTING_RULES_PATH,
) -> dict[str, Any]:
    session_id = str(row["session_id"])
    signals = read_transcript_signals(session_id, projects_dir=projects_dir)
    dominant = _dominant_model(row["model_breakdown"])
    role = _classify_role(signals)
    task_class = _classify_task(signals)
    policy_available = _routing_policy_available(routing_rules_path)
    routing_basis = _routing_basis(task_class, dominant)
    confidence = _confidence(
        signals=signals,
        task_class=task_class,
        role=role,
        dominant_model=dominant,
        routing_policy_available=policy_available,
    )

    if confidence < MIN_QUERY_CONFIDENCE and task_class == "conversation":
        task_class = "unknown"
        routing_basis = "unknown"

    return {
        "session_id": session_id,
        "role": role,
        "task_class": task_class,
        "routing_basis": routing_basis,
        "dominant_model": dominant,
        "confidence": confidence,
        "method": METHOD,
    }


def _confidence_distribution(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN confidence >= ? THEN 1 ELSE 0 END) AS high_confidence,
               SUM(CASE WHEN confidence < ? THEN 1 ELSE 0 END) AS low_confidence
        FROM session_classification
        WHERE method = ?
        """,
        (MIN_QUERY_CONFIDENCE, MIN_QUERY_CONFIDENCE, METHOD),
    ).fetchone()
    total = int(row["total"] or 0) if row else 0
    high = int(row["high_confidence"] or 0) if row else 0
    low = int(row["low_confidence"] or 0) if row else 0
    fraction = round(high / total, 4) if total else 0.0
    return {
        "total_classified": total,
        "confidence_gte_0_6": high,
        "confidence_lt_0_6": low,
        "confidence_gte_0_6_fraction": fraction,
    }


def classify_sessions(
    db_path: Path = BRIDGE_DB_PATH,
    *,
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    routing_rules_path: Path = ROUTING_RULES_PATH,
) -> dict[str, Any]:
    """Derive and upsert session_classification rows for bridge-db session_costs."""
    if not db_path.exists():
        return {"classified": 0, "skipped": 0, "errors": [f"bridge_db not found at {db_path}"]}

    conn: sqlite3.Connection | None = None
    classified = 0
    skipped = 0
    errors: list[str] = []
    try:
        conn = _connect_rw(db_path)
        if not _table_exists(conn, "session_costs"):
            return {
                "classified": 0,
                "skipped": 0,
                "errors": ["session_costs table not found; bridge-db must initialize it first"],
            }
        if not _table_exists(conn, "session_classification"):
            return {
                "classified": 0,
                "skipped": 0,
                "errors": [
                    "session_classification table not found; bridge-db schema v12 must be applied first"
                ],
            }

        rows = conn.execute(
            """
            SELECT session_id, model_breakdown
            FROM session_costs
            ORDER BY started_at DESC
            """
        ).fetchall()
        for row in rows:
            session_id = row["session_id"]
            if not session_id:
                skipped += 1
                continue
            try:
                classification = classify_session_row(
                    row,
                    projects_dir=projects_dir,
                    routing_rules_path=routing_rules_path,
                )
                conn.execute(
                    """
                    INSERT INTO session_classification (
                        session_id, role, task_class, routing_basis,
                        dominant_model, confidence, method
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        role           = excluded.role,
                        task_class     = excluded.task_class,
                        routing_basis  = excluded.routing_basis,
                        dominant_model = excluded.dominant_model,
                        confidence     = excluded.confidence,
                        method         = excluded.method,
                        classified_at  = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """,
                    (
                        classification["session_id"],
                        classification["role"],
                        classification["task_class"],
                        classification["routing_basis"],
                        classification["dominant_model"],
                        classification["confidence"],
                        classification["method"],
                    ),
                )
                classified += 1
            except (sqlite3.Error, OSError, ValueError) as exc:
                errors.append(f"session {session_id}: {exc}")
        conn.commit()
        return {
            "classified": classified,
            "skipped": skipped,
            "errors": errors,
            "confidence_distribution": _confidence_distribution(conn),
        }
    except sqlite3.OperationalError as exc:
        return {
            "classified": classified,
            "skipped": skipped,
            "errors": [f"bridge_db operational error: {exc}"],
        }
    except sqlite3.Error as exc:
        return {"classified": classified, "skipped": skipped, "errors": [f"bridge_db_error: {exc}"]}
    finally:
        if conn is not None:
            conn.close()


def cost_routing_violations(
    db_path: Path = BRIDGE_DB_PATH,
    *,
    min_confidence: float = MIN_QUERY_CONFIDENCE,
) -> dict[str, Any]:
    """Return bucket-ranked over-powered spend plus weekly directional context."""
    if not db_path.exists():
        return {"error": "bridge_db_unavailable", "detail": str(db_path)}

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_ro(db_path)
        if not _table_exists(conn, "session_costs"):
            return {"error": "missing_table", "detail": "session_costs table not found"}
        if not _table_exists(conn, "session_classification"):
            return {
                "error": "missing_table",
                "detail": "session_classification table not found; run bridge-db schema v12",
            }
        bucket_rows = conn.execute(
            f"""
            SELECT COALESCE(sc.project_name, 'unmapped') AS project_name,
                   COUNT(*) AS over_powered_rows,
                   SUM(CASE WHEN {_LIFETIME_BUCKET_SQL} THEN 1 ELSE 0 END) AS bucket_rows,
                   SUM(CASE WHEN NOT {_LIFETIME_BUCKET_SQL} THEN 1 ELSE 0 END) AS session_rows,
                   ROUND(SUM(sc.cost_usd), 2) AS wasted_usd_upper_bound
            FROM session_costs sc
            JOIN session_classification cl USING (session_id)
            WHERE cl.routing_basis = 'over-powered'
              AND cl.confidence >= ?
            GROUP BY COALESCE(sc.project_name, 'unmapped')
            ORDER BY SUM(sc.cost_usd) DESC
            """,
            (min_confidence,),
        ).fetchall()
        weekly_rows = conn.execute(
            """
            SELECT strftime('%Y-W%W', sc.started_at) AS week,
                   COUNT(*) AS over_powered_sessions,
                   ROUND(SUM(sc.cost_usd), 2) AS wasted_usd_upper_bound
            FROM session_costs sc
            JOIN session_classification cl USING (session_id)
            WHERE cl.routing_basis = 'over-powered'
              AND cl.confidence >= ?
            GROUP BY week
            ORDER BY week DESC
            """,
            (min_confidence,),
        ).fetchall()
        return {
            "confidence_threshold": min_confidence,
            "caveat": (
                "wasted_usd_upper_bound attributes full session cost to over-powered "
                "routing; rows are ccusage directory buckets, not individual sessions; "
                "a bucket's lifetime cost is attributed to its started_at; treat weekly "
                "figures as directional only, bucket ranking as the reliable view"
            ),
            "confidence_distribution": _confidence_distribution(conn),
            "top_over_powered_buckets": [
                {
                    "rank": index,
                    "project_name": row["project_name"],
                    "granularity": "bucket" if row["bucket_rows"] else "session",
                    "over_powered_rows": row["over_powered_rows"],
                    "bucket_rows": row["bucket_rows"],
                    "session_rows": row["session_rows"],
                    "wasted_usd_upper_bound": row["wasted_usd_upper_bound"],
                }
                for index, row in enumerate(bucket_rows, start=1)
            ],
            "weeks": [
                {
                    "week": row["week"],
                    "over_powered_sessions": row["over_powered_sessions"],
                    "wasted_usd_upper_bound": row["wasted_usd_upper_bound"],
                }
                for row in weekly_rows
            ],
        }
    except sqlite3.Error as exc:
        return {"error": "bridge_db_error", "detail": str(exc)}
    finally:
        if conn is not None:
            conn.close()
