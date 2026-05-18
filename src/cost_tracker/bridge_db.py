"""SQLite read/write helpers for bridge-db cost_records."""

from __future__ import annotations

import re
import sqlite3
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
    Aggregate cost_records over the last `window_days` days.

    Groups by `notes`-derived project name (first non-empty word after "project:")
    or by `system` if notes are empty/unparseable.

    Returns list of {project, total_usd, record_count} sorted by total_usd desc.
    """
    if not db_path.exists():
        return [{"error": "bridge_db_unavailable", "detail": str(db_path)}]

    # month-based window: find months within window_days
    # cost_records stores month as "YYYY-MM"; we pull recent months and filter
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=window_days)
    cutoff_month = cutoff.strftime("%Y-%m")

    try:
        conn = _connect(db_path, readonly=True)
        with conn:
            rows = conn.execute(
                """
                SELECT system, notes, amount
                FROM cost_records
                WHERE month >= ?
                ORDER BY month DESC
                """,
                (cutoff_month,),
            ).fetchall()
    except sqlite3.Error as exc:
        return [{"error": "bridge_db_error", "detail": str(exc)}]
    finally:
        conn.close()

    # Aggregate
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        project = _derive_project(row["notes"], row["system"])
        if project not in totals:
            totals[project] = {"project": project, "total_usd": 0.0, "record_count": 0}
        totals[project]["total_usd"] = round(totals[project]["total_usd"] + row["amount"], 6)
        totals[project]["record_count"] += 1

    return sorted(totals.values(), key=lambda x: x["total_usd"], reverse=True)


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
    Insert a row into cost_records.

    Returns {record_id, status: "inserted"} or {error, detail}.
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

    try:
        conn = _connect(db_path, readonly=False)
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO cost_records (system, month, amount, notes)
                VALUES (?, ?, ?, ?)
                """,
                (system, month, amount, notes),
            )
            return {"record_id": cursor.lastrowid, "status": "inserted"}
    except sqlite3.IntegrityError as exc:
        return {"error": "integrity_error", "detail": str(exc)}
    except sqlite3.Error as exc:
        return {"error": "bridge_db_error", "detail": str(exc)}
    finally:
        conn.close()
