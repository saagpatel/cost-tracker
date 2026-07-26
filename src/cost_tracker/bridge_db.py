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

# ccusage writes two different row granularities into session_costs:
#   * directory buckets   — session_id like "-Users-d": ONE row carrying that
#     directory's ALL-TIME spend, stamped with a single lastActivity date
#   * session-scoped rows — UUIDs, plus "<bucket>/<uuid>/subagents" and workflow
#     rows, each covering the spend of one session
#
# Only session-scoped rows can answer "how much in the last N days". Windowing a
# lifetime total is bimodal: a busy home-directory bucket can dominate the table,
# and against a rolling window it either drops out wholesale (leaving a few
# percent of real spend visible) or lands entirely inside it (counting months of
# history as if it happened this fortnight). Neither figure is window spend, so
# windowed queries select session rows only and the lifetime buckets are reported
# separately by cost_lifetime_buckets().
_LIFETIME_BUCKET_SQL = "(session_id GLOB '-*' AND session_id NOT GLOB '*/*')"

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
    Aggregate session-granularity session_costs over the last window_days, grouped
    by project.

    ccusage directory buckets carry all-time totals against a single date and are
    excluded — they cannot be windowed without inventing a number. When any exist,
    a trailing advisory entry reports how much was held back, so the caller can
    never silently read a windowed figure as if it were total spend. Use
    cost_lifetime_buckets() for the all-time per-project view.

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
                    f"""
                    SELECT COALESCE(project_name, '(unmapped)') AS project_name,
                           SUM(cost_usd)                        AS total_usd,
                           COUNT(*)                             AS session_count
                    FROM session_costs
                    WHERE started_at >= ?
                      AND NOT {_LIFETIME_BUCKET_SQL}
                    GROUP BY COALESCE(project_name, '(unmapped)')
                    ORDER BY total_usd DESC
                    """,
                    (cutoff,),
                ).fetchall()

                result: list[dict[str, Any]] = [
                    {
                        "project": row["project_name"],
                        "total_usd": round(row["total_usd"], 6),
                        "session_count": row["session_count"],
                    }
                    for row in rows
                ]

                held_back = conn.execute(
                    f"""
                    SELECT COUNT(*) AS bucket_count, COALESCE(SUM(cost_usd), 0) AS total_usd
                    FROM session_costs
                    WHERE {_LIFETIME_BUCKET_SQL}
                    """
                ).fetchone()
                if held_back and held_back["bucket_count"]:
                    result.append(
                        {
                            "granularity": "lifetime_excluded",
                            "bucket_count": held_back["bucket_count"],
                            "excluded_usd": round(held_back["total_usd"], 6),
                            "note": (
                                "ccusage directory buckets hold all-time spend against a "
                                "single date and cannot be windowed; excluded from the "
                                "figures above. Call cost_lifetime_buckets() for that view."
                            ),
                        }
                    )

                return result

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


def cost_lifetime_buckets(db_path: Path = BRIDGE_DB_PATH) -> list[dict[str, Any]]:
    """
    Return all-time per-project spend from ccusage directory buckets.

    This is the honest home for the rows cost_top_projects excludes. Each bucket
    is one ccusage directory rollup covering that directory's entire history, so
    these totals answer "how much has this project ever cost" — never "how much
    in the last N days". There is deliberately no window parameter: the source
    rows carry a single date for an arbitrarily long span, so any window applied
    here would be fabricated.

    Returns list of {project, total_usd, bucket_count, last_activity} sorted by
    total_usd desc.
    """
    if not db_path.exists():
        return [{"error": "bridge_db_unavailable", "detail": str(db_path)}]

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path, readonly=True)
        with conn:
            rows = conn.execute(
                f"""
                SELECT COALESCE(project_name, '(unmapped)') AS project_name,
                       SUM(cost_usd)                        AS total_usd,
                       COUNT(*)                             AS bucket_count,
                       MAX(started_at)                      AS last_activity
                FROM session_costs
                WHERE {_LIFETIME_BUCKET_SQL}
                GROUP BY COALESCE(project_name, '(unmapped)')
                ORDER BY total_usd DESC
                """
            ).fetchall()
            return [
                {
                    "project": row["project_name"],
                    "total_usd": round(row["total_usd"], 6),
                    "bucket_count": row["bucket_count"],
                    "last_activity": row["last_activity"],
                    "granularity": "lifetime",
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
