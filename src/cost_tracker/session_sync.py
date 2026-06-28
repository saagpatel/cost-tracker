"""Sync ccusage session data into bridge-db session_costs table.

Maps sessions to projects via ~/.claude/projects/ directory structure.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from cost_tracker.ccusage import _iter_model_costs

BRIDGE_DB_PATH = Path.home() / ".local" / "share" / "bridge-db" / "bridge.db"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# session_costs is owned and created by bridge-db, which holds the single,
# version-gated schema definition (bridge_db/db.py, ensure_schema). cost-tracker is
# a pure consumer: it upserts into the canonical table and never defines or creates
# the schema, so the two can no longer diverge.


def _decode_project_name(dirname: str) -> str | None:
    """
    Decode a ~/.claude/projects/<dirname> directory name to a project name.

    Claude Code encodes the absolute project path as the directory name by
    replacing each '/' with '-' (the leading '/' becomes the leading '-').
    Internal dashes in path components are preserved as-is, making the encoding
    lossy for paths with dashes. We recover the project name by anchoring on
    known parent-directory markers and taking everything after them.

    Known anchors (tried in order, longest match first):
      -Users-<user>--local-share-  → everything after is the service/project name
      -Users-<user>--claude-       → everything after is the sub-project name (skip bare -claude)
      -Users-<user>-Projects-      → everything after is the project name
      -Users-<user>-Documents-     → everything after is the document project name
      -Users-<user>-               → everything after is the top-level project name
      -private-                    → skip (tmp / system paths)

    Returns None for empty results, single-char results, 'd', or 'tmp'.
    """
    if not dirname:
        return None

    # Skip clearly system/temp paths
    if dirname.startswith("-private-"):
        return None

    # Known anchor patterns (order matters — most-specific first)
    _ANCHOR_PATTERNS = [
        "--local-share-",  # ~/.local/share/<service>
        "--claude-",  # ~/.claude/<sub>
        "-Projects-",  # ~/Projects/<project>
        "-Documents-",  # ~/Documents/<project>
    ]

    # Strip leading '-Users-<user>' prefix first
    # The dirname starts like: -Users-d-... or -Users-saagar-...
    s = dirname
    if s.startswith("-"):
        s = s[1:]  # drop leading dash to get: Users-d-...

    # Drop the "Users-<username>-" prefix
    parts = s.split("-", 2)  # ["Users", "<user>", "rest..."]
    if len(parts) < 3 or parts[0].lower() != "users":
        # Not a home-path; fall back to simple last-segment approach
        segs = [p for p in dirname.replace("-", "/").split("/") if p]
        if not segs:
            return None
        candidate = segs[-1]
        return candidate if len(candidate) > 1 and candidate.lower() not in ("d", "tmp") else None

    remainder = "-" + parts[2]  # restore leading dash for anchor matching

    # Try anchors
    for anchor in _ANCHOR_PATTERNS:
        idx = remainder.find(anchor)
        if idx != -1:
            project = remainder[idx + len(anchor) :]
            if not project:
                return None
            # Clean up: skip if the result looks like a bare config dir
            if project in ("claude",):
                return None
            if len(project) <= 1 or project.lower() == "d":
                return None
            return project

    # No anchor matched — the remainder after the username IS the project name
    # e.g. -Users-d-Notion → remainder = '-Notion' → strip leading dash
    project = remainder.lstrip("-")
    if not project or len(project) <= 1 or project.lower() in ("d", "tmp", "claude"):
        return None
    return project


def _build_session_project_map(projects_dir: Path = CLAUDE_PROJECTS_DIR) -> dict[str, str]:
    """
    Scan ~/.claude/projects/ and build {session_id: project_name} mapping.

    Each subdirectory encodes the project path; each .jsonl file is named
    <session-uuid>.jsonl.
    """
    mapping: dict[str, str] = {}
    if not projects_dir.is_dir():
        return mapping

    for subdir in projects_dir.iterdir():
        if not subdir.is_dir():
            continue
        project_name = _decode_project_name(subdir.name)
        if project_name is None:
            continue
        for jsonl_file in subdir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            mapping[session_id] = project_name

    return mapping


def _run_ccusage() -> list[dict[str, Any]] | None:
    """Run `ccusage session --json` and return parsed sessions list."""
    try:
        result = subprocess.run(
            ["ccusage", "session", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        raw = json.loads(result.stdout.strip())
        # ccusage session --json returns {"sessions": [...]}
        if isinstance(raw, dict):
            return raw.get("sessions", [])
        # Older formats: [["session", [...]]] — handle defensively
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, list) and len(item) == 2 and item[0] == "session":
                    inner = item[1]
                    if isinstance(inner, list):
                        return inner
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:  # noqa: BLE001
        return None


def _connect_rw(path: Path) -> sqlite3.Connection:
    # mode=rw, not rwc: bridge-db owns creation of the DB file and its schema.
    # Callers guard db_path.exists() before connecting.
    uri = f"file:{path}?mode=rw"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sync_session_costs(
    db_path: Path = BRIDGE_DB_PATH,
    ccusage_fn: Any = None,
) -> dict[str, Any]:
    """
    Sync ccusage session data into bridge-db session_costs table.

    Args:
        db_path: Path to bridge.db (defaults to BRIDGE_DB_PATH).
        ccusage_fn: Optional callable returning sessions list (for testing).
                    Defaults to real ccusage subprocess call.

    Returns:
        {"synced": N, "skipped": K, "errors": [...]}
    """
    session_to_project = _build_session_project_map()

    fetch = ccusage_fn if ccusage_fn is not None else _run_ccusage
    sessions = fetch()
    if sessions is None:
        return {"synced": 0, "skipped": 0, "errors": ["ccusage failed or unavailable"]}

    if not db_path.exists():
        return {
            "synced": 0,
            "skipped": 0,
            "errors": [f"bridge_db not found at {db_path}"],
        }

    conn: sqlite3.Connection | None = None
    synced = 0
    skipped = 0
    errors: list[str] = []

    try:
        conn = _connect_rw(db_path)

        # bridge-db owns the session_costs schema; cost-tracker does not create it.
        # Fail cleanly if bridge-db has not initialized the table yet.
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='session_costs'"
            ).fetchone()
            is None
        ):
            return {
                "synced": 0,
                "skipped": 0,
                "errors": [
                    "session_costs table not found in bridge.db — bridge-db owns and "
                    "creates this table; ensure it has initialized its schema first"
                ],
            }

        for session in sessions:
            session_id = session.get("period") or session.get("sessionId")
            if not session_id:
                skipped += 1
                continue

            # Two ccusage formats:
            # - newer (npx ccusage@latest): period=UUID → look up via filesystem map
            # - installed ccusage: sessionId=dir-name → decode directly
            if session.get("period"):
                project_name = session_to_project.get(session_id)
            else:
                project_name = _decode_project_name(session_id)

            metadata = session.get("metadata", {})
            started_at = metadata.get("lastActivity") or session.get("lastActivity", "")

            cost_usd = session.get("totalCost", 0.0)

            model_breakdown: dict[str, float] = {}
            for name, cost in _iter_model_costs(session.get("modelBreakdowns")):
                model_breakdown[name or "unknown"] = round(cost, 6)

            try:
                conn.execute(
                    """
                    INSERT INTO session_costs
                        (session_id, project_name, started_at, cost_usd, model_breakdown, source)
                    VALUES (?, ?, ?, ?, ?, 'cc')
                    ON CONFLICT(session_id) DO UPDATE SET
                        cost_usd        = excluded.cost_usd,
                        model_breakdown = excluded.model_breakdown,
                        project_name    = COALESCE(excluded.project_name, project_name),
                        recorded_at     = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """,
                    (
                        session_id,
                        project_name,
                        started_at,
                        cost_usd,
                        json.dumps(model_breakdown),
                    ),
                )
                synced += 1
            except sqlite3.Error as exc:
                errors.append(f"session {session_id}: {exc}")

        conn.commit()

    except sqlite3.OperationalError as exc:
        return {
            "synced": synced,
            "skipped": skipped,
            "errors": [f"bridge_db operational error: {exc}"],
        }
    except sqlite3.Error as exc:
        return {"synced": synced, "skipped": skipped, "errors": [f"bridge_db_error: {exc}"]}
    finally:
        if conn is not None:
            conn.close()

    return {"synced": synced, "skipped": skipped, "errors": errors}
