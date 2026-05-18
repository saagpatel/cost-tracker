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


def _extract_by_model(breakdowns: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate model breakdowns into {family: total_cost}."""
    totals: dict[str, float] = {}
    for b in breakdowns:
        family = _model_family(b.get("modelName", ""))
        totals[family] = round(totals.get(family, 0.0) + b.get("cost", 0.0), 6)
    return totals


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

    try:
        data: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {"error": _UNAVAILABLE_ERROR, "detail": f"JSON parse error: {exc}"}

    daily_list: list[dict[str, Any]] = data.get("daily", [])
    today_str = date.today().isoformat()

    # ccusage may return one or more days; find today's entry
    entry = next((d for d in daily_list if d.get("date") == today_str), None)
    if entry is None:
        # No activity today yet
        return {
            "date": today_str,
            "total_usd": 0.0,
            "by_model": {},
            "session_count": 0,
        }

    return {
        "date": entry["date"],
        "total_usd": round(entry.get("totalCost", 0.0), 6),
        "by_model": _extract_by_model(entry.get("modelBreakdowns", [])),
        "session_count": len(entry.get("modelsUsed", [])),
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

    try:
        data: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {"error": _UNAVAILABLE_ERROR, "detail": f"JSON parse error: {exc}"}

    sessions: list[dict[str, Any]] = data.get("sessions", [])
    if not sessions:
        return {
            "session_id": None,
            "started_at": date.today().isoformat(),
            "current_usd": 0.0,
            "by_model": {},
        }

    # Most-recently-active session (last in list after --since filter)
    current = sessions[-1]
    return {
        "session_id": current.get("sessionId"),
        "started_at": current.get("lastActivity", date.today().isoformat()),
        "current_usd": round(current.get("totalCost", 0.0), 6),
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

    try:
        data: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [{"error": _UNAVAILABLE_ERROR, "detail": f"JSON parse error: {exc}"}]

    monthly_list: list[dict[str, Any]] = data.get("monthly", [])
    result = [
        {
            "month": entry["month"],
            "total_usd": round(entry.get("totalCost", 0.0), 6),
            "by_model": _extract_by_model(entry.get("modelBreakdowns", [])),
        }
        for entry in monthly_list
    ]
    # Sort oldest first (ccusage default is asc already, but be explicit)
    result.sort(key=lambda x: x["month"])
    return result
