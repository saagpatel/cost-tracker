"""SQLite read/write helpers for bridge-db cost_records."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

BRIDGE_DB_PATH = Path.home() / ".local" / "share" / "bridge-db" / "bridge.db"

VALID_SYSTEMS = frozenset({"cc", "codex", "claude_ai", "notion_os", "personal_ops"})
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# The live DB CHECK constraint only covers a subset of systems (no claude_ai).
# We enforce the full set here and let the DB reject anything else.
# When inserting claude_ai, the DB will raise; callers should handle that.


def _connect(path: Path = BRIDGE_DB_PATH, *, readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{path}?mode={'ro' if readonly else 'rwc'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cost_top_projects(
    window_days: int = 14, db_path: Path = BRIDGE_DB_PATH
) -> list[dict[str, Any]]:
    """
    Aggregate session_costs over the last window_days, grouped by project.

    Falls back to cost_records system totals if session_costs table doesn't exist,
    returning a note to run sync first.

    Returns list of {project, total_usd, session_count} sorted by total_usd desc.
    """
    if not db_path.exists():
        return [{"error": "bridge_db_unavailable", "detail": str(db_path)}]

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path, readonly=True)
        with conn:
            try:
                rows = conn.execute(
                    """
                    SELECT project_name, SUM(cost_usd) as total_usd, COUNT(*) as session_count
                    FROM session_costs
                    WHERE started_at >= ?
                      AND project_name IS NOT NULL
                    GROUP BY project_name
                    ORDER BY total_usd DESC
                    """,
                    (cutoff,),
                ).fetchall()

                if not rows:
                    # Table exists but no data — include unmapped sessions
                    unmapped = conn.execute(
                        """
                        SELECT SUM(cost_usd) as total_usd, COUNT(*) as session_count
                        FROM session_costs
                        WHERE started_at >= ?
                          AND project_name IS NULL
                        """,
                        (cutoff,),
                    ).fetchone()
                    result = []
                    if unmapped and unmapped["total_usd"]:
                        result.append(
                            {
                                "project": "(unmapped)",
                                "total_usd": round(unmapped["total_usd"], 6),
                                "session_count": unmapped["session_count"],
                            }
                        )
                    return result

                result = []
                for row in rows:
                    result.append(
                        {
                            "project": row["project_name"],
                            "total_usd": round(row["total_usd"], 6),
                            "session_count": row["session_count"],
                        }
                    )

                # Also include unmapped sessions
                unmapped = conn.execute(
                    """
                    SELECT SUM(cost_usd) as total_usd, COUNT(*) as session_count
                    FROM session_costs
                    WHERE started_at >= ?
                      AND project_name IS NULL
                    """,
                    (cutoff,),
                ).fetchone()
                if unmapped and unmapped["total_usd"]:
                    result.append(
                        {
                            "project": "(unmapped)",
                            "total_usd": round(unmapped["total_usd"], 6),
                            "session_count": unmapped["session_count"],
                        }
                    )

                return sorted(result, key=lambda x: x["total_usd"], reverse=True)

            except sqlite3.OperationalError:
                # session_costs table doesn't exist — fall back to cost_records
                pass

            # Fallback: aggregate cost_records by system
            cutoff_month = (date.today() - timedelta(days=window_days)).strftime("%Y-%m")
            rows = conn.execute(
                """
                SELECT system, SUM(amount) as total_usd, COUNT(*) as record_count
                FROM cost_records
                WHERE month >= ?
                GROUP BY system
                ORDER BY total_usd DESC
                """,
                (cutoff_month,),
            ).fetchall()

            return [
                {
                    "system": row["system"],
                    "total_usd": round(row["total_usd"], 6),
                    "note": "session_costs table not yet populated — run sync first",
                }
                for row in rows
            ]

    except sqlite3.Error as exc:
        return [{"error": "bridge_db_error", "detail": str(exc)}]
    finally:
        if conn is not None:
            conn.close()


def latest_cost_record(
    system: str = "cc", month: str | None = None, db_path: Path = BRIDGE_DB_PATH
) -> dict[str, Any]:
    """
    Return the persisted bridge-db cost row for a system/month.

    Defaults to the current calendar month. This is read-only and intended for
    stale-report detection against live ccusage output.
    """
    if month is None:
        month = date.today().strftime("%Y-%m")

    if not db_path.exists():
        return {"error": "bridge_db_unavailable", "detail": str(db_path)}

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path, readonly=True)
        with conn:
            row = conn.execute(
                """
                SELECT system, month, amount, notes, recorded_at
                FROM cost_records
                WHERE system = ? AND month = ?
                """,
                (system, month),
            ).fetchone()
    except sqlite3.Error as exc:
        return {"error": "bridge_db_error", "detail": str(exc)}
    finally:
        if conn is not None:
            conn.close()

    if row is None:
        return {"system": system, "month": month, "exists": False}

    return {
        "system": row["system"],
        "month": row["month"],
        "exists": True,
        "amount_usd": round(row["amount"], 6),
        "notes": row["notes"],
        "recorded_at": row["recorded_at"],
    }


def _derive_project(notes: str | None, system: str) -> str:
    """Extract a project label from notes, falling back to system name."""
    if not notes:
        return system
    # Look for "project:<name>" pattern
    match = re.search(r"project[:\s]+([A-Za-z0-9_\-]+)", notes, re.IGNORECASE)
    if match:
        return match.group(1)
    # Use first significant token from notes (skip short connector words)
    tokens = [t for t in notes.split() if len(t) > 3]
    if tokens:
        return tokens[0].rstrip(".,;:")
    return system


def insert_cost_record(
    month: str,
    amount: float,
    system: str,
    notes: str | None = None,
    db_path: Path = BRIDGE_DB_PATH,
) -> dict[str, Any]:
    """
    Upsert a row into cost_records on (system, month).

    Matches bridge-db's record_cost owner semantics (ON CONFLICT DO UPDATE).
    Returns {record_id, status} where status is "inserted" or "updated",
    or {error, detail} on failure.
    """
    # Validate inputs before touching the DB
    if not _MONTH_RE.match(month):
        return {"error": "validation_error", "detail": f"month must be YYYY-MM, got: {month!r}"}

    if system not in VALID_SYSTEMS:
        return {
            "error": "validation_error",
            "detail": f"system must be one of {sorted(VALID_SYSTEMS)}, got: {system!r}",
        }

    if amount < 0:
        return {"error": "validation_error", "detail": f"amount must be >= 0, got: {amount}"}

    if not db_path.exists():
        return {"error": "bridge_db_unavailable", "detail": str(db_path)}

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path, readonly=False)
        with conn:
            existing = conn.execute(
                "SELECT id FROM cost_records WHERE system = ? AND month = ?",
                (system, month),
            ).fetchone()
            # Upsert to match bridge-db's record_cost owner semantics
            # (ON CONFLICT(system, month) DO UPDATE) instead of raising on duplicates.
            cursor = conn.execute(
                """
                INSERT INTO cost_records (system, month, amount, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(system, month) DO UPDATE SET
                    amount = excluded.amount,
                    notes = excluded.notes,
                    recorded_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """,
                (system, month, amount, notes),
            )
            # record_id: on update the row keeps its id; on insert use the new rowid.
            # status is advisory (the cost-recording path is single-writer in practice).
            record_id = existing[0] if existing else cursor.lastrowid
            return {
                "record_id": record_id,
                "status": "updated" if existing else "inserted",
            }
    except sqlite3.Error as exc:
        return {"error": "bridge_db_error", "detail": str(exc)}
    finally:
        if conn is not None:
            conn.close()
