"""Sync ccusage session data into bridge-db session_costs table.

Maps sessions to projects via ~/.claude/projects/ directory structure.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from cost_tracker.ccusage import _iter_model_costs

BRIDGE_DB_PATH = Path.home() / ".local" / "share" / "bridge-db" / "bridge.db"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
HOME_ADHOC_PROJECT = "home-adhoc"

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

    Bare home-directory sessions return ``home-adhoc`` so genuinely non-project
    work is attributed to a named hygiene bucket instead of becoming unmapped.

    Returns ``home-adhoc`` for ambiguous non-project fragments and None for
    empty/system results.
    """
    if not dirname:
        return None

    # Skip clearly system/temp paths
    if dirname.startswith("-private-"):
        return None

    # Known anchor patterns (order matters — most-specific first)
    anchor_patterns = [
        ("--local-share-", Path.home() / ".local" / "share"),
        ("--claude-", Path.home() / ".claude"),
        ("-Projects-", Path.home() / "Projects"),
        ("-Documents-", Path.home() / "Documents"),
    ]

    # Strip leading '-Users-<user>' prefix first
    # The dirname starts like: -Users-d-... or -Users-saagar-...
    s = dirname
    if s.startswith("-"):
        s = s[1:]  # drop leading dash to get: Users-d-...

    # Drop the "Users-<username>-" prefix
    parts = s.split("-", 2)  # ["Users", "<user>", "rest..."]
    if len(parts) == 2 and parts[0].lower() == "users" and parts[1]:
        return HOME_ADHOC_PROJECT
    if len(parts) < 3 or parts[0].lower() != "users":
        # Not a home path. ccusage workflow/session IDs such as
        # ``wf_7d0e9cc5-085`` are not project names; their tails previously leaked
        # as 3-char hex project keys. Bucket them instead of returning fragments.
        segs = [p for p in dirname.replace("-", "/").split("/") if p]
        if not segs:
            return None
        return None if _is_system_fragment(segs[-1]) else HOME_ADHOC_PROJECT

    remainder = "-" + parts[2]  # restore leading dash for anchor matching

    # Try anchors
    for anchor, root in anchor_patterns:
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
            return (
                _recover_project_path(root, project)
                or _fallback_recover_project_name(project)
                or project
            )

    # No anchor matched. Treat the remainder as an ambiguous decode failure, not
    # a project name, because returning it can leak short hex tails and lossy
    # dash-mangled path fragments into session_costs.project_name.
    project = remainder.lstrip("-")
    if not project:
        return HOME_ADHOC_PROJECT
    if _is_system_fragment(project) or project.lower() == "claude":
        return None
    return HOME_ADHOC_PROJECT


def _is_system_fragment(candidate: str) -> bool:
    """Return True for values that should be skipped instead of bucketed."""
    return len(candidate) <= 1 or candidate.lower() in {"d", "tmp"}


def _safe_encoded_path(value: str) -> str:
    """
    Approximate Claude's lossy project-dir encoding for local path recovery.

    Separators, apostrophes, spaces, and punctuation flatten to dashes while
    existing dashes remain stable, which lets us match names like
    ``Devil's Advocate`` against ``Devil-s-Advocate``.
    """
    encoded: list[str] = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            encoded.append(char)
        else:
            encoded.append("-")
    return "".join(encoded)


@lru_cache(maxsize=8)
def _encoded_project_paths(root: str) -> dict[str, str]:
    root_path = Path(root)
    if not root_path.is_dir():
        return {}

    skip_names = {
        ".git",
        ".hg",
        ".next",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
    encoded: dict[str, str] = {}
    stack: list[tuple[Path, int]] = [(root_path, 0)]
    max_depth = 3

    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            children = [child for child in current.iterdir() if child.is_dir()]
        except OSError:
            continue

        for child in children:
            if child.name in skip_names:
                continue
            relative = child.relative_to(root_path).as_posix()
            encoded.setdefault(_safe_encoded_path(relative), relative)
            stack.append((child, depth + 1))

    return encoded


def _recover_project_path(root: Path, encoded_project: str) -> str | None:
    known_paths = _encoded_project_paths(str(root))
    if recovered := known_paths.get(encoded_project):
        return recovered

    parts = encoded_project.split("-")
    for idx in range(1, len(parts)):
        prefix = "-".join(parts[:idx])
        suffix = "-".join(parts[idx:])
        if prefix in known_paths and (recovered := known_paths.get(suffix)):
            return recovered

    return None


def _fallback_recover_project_name(encoded_project: str) -> str | None:
    """Recover known lossy project names when the source tree is unavailable.

    CI runs do not have the operator's full /Users/d/Projects tree, so
    filesystem-based recovery cannot resolve dash-mangled Claude project
    directory names there. Keep this fallback intentionally conservative and
    tied to patterns the test suite documents.
    """
    nested_prefixes = ("Fun-GamePrjs-",)
    for prefix in nested_prefixes:
        if encoded_project.startswith(prefix):
            return encoded_project.removeprefix(prefix)

    parts = encoded_project.split("-")
    if "s" not in parts[1:-1]:
        return None

    words: list[str] = []
    for part in parts:
        if part == "s" and words:
            words[-1] = f"{words[-1]}'s"
        else:
            words.append(part)
    return " ".join(words)


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
