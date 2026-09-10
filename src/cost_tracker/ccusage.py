"""Subprocess wrapper around the ccusage CLI."""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from typing import Any

_UNAVAILABLE_ERROR = "ccusage_unavailable"


def _run(args: list[str]) -> tuple[str | None, str | None]:
    """Run ccusage with the given args. Returns (stdout, error_detail)."""
    try:
        result = subprocess.run(
            ["ccusage", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            return None, detail
        return result.stdout.strip(), None
    except FileNotFoundError:
        return None, "ccusage binary not found on PATH"
    except subprocess.TimeoutExpired:
        return None, "ccusage timed out after 30s"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _model_family(model_name: str) -> str:
    """Map a full model name to a short family key (opus/sonnet/haiku/other)."""
    lower = model_name.lower()
    if "opus" in lower:
        return "opus"
    if "sonnet" in lower:
        return "sonnet"
    if "haiku" in lower:
        return "haiku"
    return "other"


def _iter_model_costs(breakdowns: object) -> list[tuple[str, float]]:
    """Parse a ccusage ``modelBreakdowns`` value into (model_name, cost) pairs.

    Tolerates a null/non-list value, non-dict entries, and non-numeric costs so a
    malformed ccusage payload degrades to partial data instead of raising.
    """
    pairs: list[tuple[str, float]] = []
    if not isinstance(breakdowns, list):
        return pairs
    for entry in breakdowns:
        if not isinstance(entry, dict):
            continue
        name = entry.get("modelName", "")
        if not isinstance(name, str):
            name = ""
        try:
            cost = float(entry.get("cost", 0.0))
        except (TypeError, ValueError):
            cost = 0.0
        pairs.append((name, cost))
    return pairs


def _extract_by_model(breakdowns: object) -> dict[str, float]:
    """Aggregate model breakdowns into {family: total_cost}."""
    totals: dict[str, float] = {}
    for name, cost in _iter_model_costs(breakdowns):
        family = _model_family(name)
        totals[family] = round(totals.get(family, 0.0) + cost, 6)
    return totals


def _since_yyyymmdd(days: int) -> str:
    start = date.today() - timedelta(days=max(days, 1) - 1)
    return start.strftime("%Y%m%d")


def _positive_limit(limit: int) -> int:
    return max(limit, 0)


def _load_object(stdout: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse ccusage JSON that must be a top-level object.

    Wrong envelopes (arrays, null, scalars) become an unavailable detail rather
    than an AttributeError on ``.get`` or invented usage rows.
    """
    if stdout is None:
        return None, "JSON parse error: empty output"
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    if not isinstance(data, dict):
        kind = "null" if data is None else type(data).__name__
        return None, f"unexpected JSON shape: {kind}"
    return data, None


def _object_entries(value: object) -> list[dict[str, Any]]:
    """Return dict entries from a list-shaped ccusage field; else empty."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _cost_usd(entry: dict[str, Any]) -> float | None:
    """Return a numeric ``totalCost``, or None when the field is unusable.

    A missing key keeps the stable empty 0.0. Wrong types are not coerced into
    a fake spend figure.
    """
    if "totalCost" not in entry:
        return 0.0
    value = entry["totalCost"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _token_count(entry: dict[str, Any]) -> int | float:
    if "totalTokens" not in entry:
        return 0
    value = entry["totalTokens"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _models_used(entry: dict[str, Any]) -> list[Any]:
    value = entry.get("modelsUsed", [])
    if not isinstance(value, list):
        return []
    return value


def _usable_entries(value: object) -> list[dict[str, Any]]:
    """Dict entries whose ``totalCost`` is missing or numeric."""
    entries: list[dict[str, Any]] = []
    for entry in _object_entries(value):
        if _cost_usd(entry) is None:
            continue
        entries.append(entry)
    return entries


def _rounded_cost(entry: dict[str, Any]) -> float:
    cost = _cost_usd(entry)
    return round(0.0 if cost is None else cost, 6)


def _entry_summary(entry: dict[str, Any], period_key: str) -> dict[str, Any] | None:
    period = entry.get(period_key)
    if not isinstance(period, str):
        return None
    cost = _cost_usd(entry)
    if cost is None:
        return None
    return {
        period_key: period,
        "total_usd": round(cost, 6),
        "total_tokens": _token_count(entry),
        "by_model": _extract_by_model(entry.get("modelBreakdowns", [])),
        "models_used": _models_used(entry),
    }


def _summarize_entries(value: object, period_key: str) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for entry in _object_entries(value):
        summary = _entry_summary(entry, period_key)
        if summary is not None:
            summaries.append(summary)
    return summaries


def _useful_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    if not label or label.lower() == "unknown project":
        return None
    return label


def _session_project(entry: dict[str, Any]) -> str | None:
    project_path = _useful_label(entry.get("projectPath"))
    if project_path is not None:
        return project_path

    project = _useful_label(entry.get("project"))
    if project is not None:
        return project

    return entry.get("sessionId")


def cost_today() -> dict[str, Any]:
    """
    Call `ccusage daily --json` and return today's entry.

    Returns:
        {date, total_usd, by_model: {opus: X, sonnet: Y, haiku: Z}, session_count}
        or {error: "ccusage_unavailable", detail: "..."}
    """
    today = date.today().strftime("%Y%m%d")
    stdout, err = _run(["daily", "--json", "--since", today, "--until", today])
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    data, err = _load_object(stdout)
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    today_str = date.today().isoformat()

    # ccusage may return one or more days; find today's entry
    entry = next(
        (d for d in _usable_entries(data.get("daily", [])) if d.get("date") == today_str), None
    )
    if entry is None:
        # No activity today yet
        return {
            "date": today_str,
            "total_usd": 0.0,
            "by_model": {},
            "session_count": 0,
        }

    return {
        "date": today_str,
        "total_usd": _rounded_cost(entry),
        "by_model": _extract_by_model(entry.get("modelBreakdowns", [])),
        "session_count": len(_models_used(entry)),
    }


def cost_session() -> dict[str, Any]:
    """
    Call `ccusage session --json` and return an aggregated view of today's sessions.

    Returns:
        {session_id, started_at, current_usd, by_model: {...}}
        or {error: "ccusage_unavailable", detail: "..."}

    Note: ccusage session groups by project path, not a running session ID.
    We return the most-recently-active session as the "current" one.
    """
    today = date.today().strftime("%Y%m%d")
    stdout, err = _run(["session", "--json", "--since", today])
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    data, err = _load_object(stdout)
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    sessions = _usable_entries(data.get("sessions", []))
    if not sessions:
        return {
            "session_id": None,
            "started_at": date.today().isoformat(),
            "current_usd": 0.0,
            "by_model": {},
        }

    # Most-recently-active usable session (last in list after --since filter)
    current = sessions[-1]
    return {
        "session_id": current.get("sessionId"),
        "started_at": current.get("lastActivity", date.today().isoformat()),
        "current_usd": _rounded_cost(current),
        "by_model": _extract_by_model(current.get("modelBreakdowns", [])),
    }


def cost_monthly_trend(months: int = 3) -> list[dict[str, Any]]:
    """
    Call `ccusage monthly --json` for the last N months.

    Returns list of {month, total_usd, by_model} sorted oldest first,
    or [{error, detail}] on failure.
    """
    since_date = date.today().replace(day=1) - timedelta(days=1)
    # Go back N-1 more months
    for _ in range(months - 1):
        since_date = since_date.replace(day=1) - timedelta(days=1)
    since_str = since_date.replace(day=1).strftime("%Y%m%d")

    stdout, err = _run(["monthly", "--json", "--since", since_str])
    if err is not None:
        return [{"error": _UNAVAILABLE_ERROR, "detail": err}]

    data, err = _load_object(stdout)
    if err is not None:
        return [{"error": _UNAVAILABLE_ERROR, "detail": err}]

    result: list[dict[str, Any]] = []
    for entry in _usable_entries(data.get("monthly", [])):
        month = entry.get("month")
        if not isinstance(month, str):
            continue
        result.append(
            {
                "month": month,
                "total_usd": _rounded_cost(entry),
                "by_model": _extract_by_model(entry.get("modelBreakdowns", [])),
            }
        )
    # Sort oldest first (ccusage default is asc already, but be explicit)
    result.sort(key=lambda x: x["month"])
    return result


def cost_month_to_date() -> dict[str, Any]:
    """
    Return the current month-to-date Claude Code spend from ccusage.

    Shape: {month, total_usd, total_tokens, by_model, models_used}
    """
    month = date.today().strftime("%Y-%m")
    since = date.today().replace(day=1).strftime("%Y%m%d")
    stdout, err = _run(["monthly", "--compact", "--json", "--since", since])
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    data, err = _load_object(stdout)
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    entry = next(
        (m for m in _usable_entries(data.get("monthly", [])) if m.get("month") == month),
        None,
    )
    if entry is None:
        return {
            "month": month,
            "total_usd": 0.0,
            "total_tokens": 0,
            "by_model": {},
            "models_used": [],
        }

    summary = _entry_summary(entry, "month")
    if summary is None:
        return {
            "month": month,
            "total_usd": 0.0,
            "total_tokens": 0,
            "by_model": {},
            "models_used": [],
        }
    return summary


def cost_top_days(days: int = 14, limit: int = 10) -> list[dict[str, Any]]:
    """
    Return recent daily spend sorted by cost descending.

    Each entry: {date, total_usd, total_tokens, by_model, models_used}
    """
    stdout, err = _run(["daily", "--compact", "--json", "--since", _since_yyyymmdd(days)])
    if err is not None:
        return [{"error": _UNAVAILABLE_ERROR, "detail": err}]

    data, err = _load_object(stdout)
    if err is not None:
        return [{"error": _UNAVAILABLE_ERROR, "detail": err}]

    days_out = _summarize_entries(data.get("daily", []), "date")
    return sorted(days_out, key=lambda x: x["total_usd"], reverse=True)[: _positive_limit(limit)]


def cost_top_sessions(window_days: int = 14, limit: int = 10) -> dict[str, Any]:
    """
    Return high-cost ccusage session/project groups with an attribution caveat.

    ccusage session output groups by session/project path and may include the
    whole session cost when the session had recent activity. Treat it as a
    workflow attribution signal, not exact current-window spend.
    """
    stdout, err = _run(["session", "--compact", "--json", "--since", _since_yyyymmdd(window_days)])
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    data, err = _load_object(stdout)
    if err is not None:
        return {"error": _UNAVAILABLE_ERROR, "detail": err}

    sessions = []
    for entry in _usable_entries(data.get("sessions", [])):
        sessions.append(
            {
                "session_id": entry.get("sessionId"),
                "project": _session_project(entry),
                "last_activity": entry.get("lastActivity"),
                "total_usd": _rounded_cost(entry),
                "total_tokens": _token_count(entry),
                "by_model": _extract_by_model(entry.get("modelBreakdowns", [])),
                "models_used": _models_used(entry),
            }
        )
    return {
        "window_days": window_days,
        "attribution": "workflow_signal_not_invoice_window",
        "sessions": sorted(sessions, key=lambda x: x["total_usd"], reverse=True)[
            : _positive_limit(limit)
        ],
    }
