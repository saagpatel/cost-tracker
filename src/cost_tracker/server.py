"""MCP server exposing 6 cost-tracking tools."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from cost_tracker import bridge_db as _bridge_db
from cost_tracker import ccusage as _ccusage
from cost_tracker import thresholds as _thresholds

app = FastMCP(
    name="cost-tracker",
    instructions=(
        "Provides live cost visibility for Claude Code sessions via ccusage CLI "
        "and bridge-db cost_records. Six tools: cost_today, cost_session, "
        "cost_monthly_trend, cost_top_projects, cost_alert_thresholds_check, cost_record."
    ),
)


@app.tool()
def cost_today() -> dict[str, Any]:
    """
    Return today's total Claude Code spend and per-model breakdown.

    Shape: {date, total_usd, by_model: {opus, sonnet, haiku, ...}, session_count}
    Returns {error: 'ccusage_unavailable', detail: '...'} if ccusage is missing or fails.
    """
    return _ccusage.cost_today()


@app.tool()
def cost_session() -> dict[str, Any]:
    """
    Return the most-recently-active session's running cost for today.

    Shape: {session_id, started_at, current_usd, by_model: {...}}
    Returns {error: 'ccusage_unavailable', detail: '...'} on failure.
    """
    return _ccusage.cost_session()


@app.tool()
def cost_monthly_trend(months: int = 3) -> list[dict[str, Any]]:
    """
    Return per-month spend for the last N months, oldest first.

    Args:
        months: Number of calendar months to look back (default 3).

    Each entry: {month: 'YYYY-MM', total_usd, by_model: {...}}
    Returns [{error, detail}] on failure.
    """
    return _ccusage.cost_monthly_trend(months=months)


@app.tool()
def cost_top_projects(window_days: int = 14) -> list[dict[str, Any]]:
    """
    Return bridge-db cost_records aggregated by project, highest spend first.

    Args:
        window_days: Rolling window in days (default 14). Filters by month >= cutoff month.

    Each entry: {project, total_usd, record_count}
    Returns [{error, detail}] if bridge-db is unavailable.
    """
    return _bridge_db.cost_top_projects(window_days=window_days)


@app.tool()
def cost_alert_thresholds_check() -> dict[str, Any]:
    """
    Check today's spend against alert thresholds.

    Thresholds default to [5, 15, 30] USD. Override via
    ~/.config/cost-tracker/thresholds.toml with:
        thresholds_usd = [5, 15, 30, 50]

    Returns:
        {today_usd, thresholds_crossed: [...], next_threshold, headroom_usd}
    """
    today_result = _ccusage.cost_today()
    if "error" in today_result:
        return today_result  # propagate ccusage failure

    today_usd = today_result.get("total_usd", 0.0)
    return _thresholds.check_thresholds(today_usd)


@app.tool()
def cost_record(
    month: Annotated[str, "Calendar month in YYYY-MM format"],
    amount: Annotated[float, "Cost in USD (>= 0)"],
    system: Annotated[
        str,
        "One of: cc, codex, claude_ai, notion_os, personal_ops",
    ],
    notes: Annotated[str | None, "Optional free-text notes"] = None,
) -> dict[str, Any]:
    """
    Insert a cost record into bridge-db.

    Args:
        month: "YYYY-MM" format.
        amount: Cost in USD, must be >= 0.
        system: One of cc | codex | claude_ai | notion_os | personal_ops.
        notes: Optional annotation (e.g. source, project name).

    Returns:
        {record_id, status: "inserted"} or {error, detail} on failure.
    """
    return _bridge_db.insert_cost_record(
        month=month,
        amount=amount,
        system=system,
        notes=notes,
    )
