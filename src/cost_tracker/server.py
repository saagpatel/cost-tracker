"""MCP server exposing local cost-tracking tools."""

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
        "and bridge-db cost_records. Live ccusage values are actuals; Codex/OpenAI "
        "local counters should be treated as directional unless provider billing is imported."
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
def cost_month_to_date() -> dict[str, Any]:
    """
    Return current month-to-date Claude Code spend from live ccusage.

    Shape: {month, total_usd, total_tokens, by_model, models_used}
    """
    return _ccusage.cost_month_to_date()


@app.tool()
def cost_top_days(days: int = 14, limit: int = 10) -> list[dict[str, Any]]:
    """
    Return recent daily Claude Code spend sorted by cost descending.

    Args:
        days: Number of recent days to inspect.
        limit: Maximum rows to return.
    """
    return _ccusage.cost_top_days(days=days, limit=limit)


@app.tool()
def cost_top_sessions(window_days: int = 14, limit: int = 10) -> dict[str, Any]:
    """
    Return high-cost workflow/session groups from ccusage.

    The result includes an attribution caveat because ccusage session grouping
    is a workflow signal, not exact invoice-window spend.
    """
    return _ccusage.cost_top_sessions(window_days=window_days, limit=limit)


@app.tool()
def cost_bridge_staleness() -> dict[str, Any]:
    """
    Compare live current-month ccusage with the persisted bridge-db cost row.

    This is a local visibility check. It does not mutate bridge-db and does not
    define the scheduled cost-oracle refresh policy.

    Shape:
        {
          month, live_total_usd, persisted_total_usd, delta_usd,
          delta_exceeds_threshold, stale_reason, persisted_recorded_at, notes
        }
    """
    live = _ccusage.cost_month_to_date()
    if "error" in live:
        return live

    month = live["month"]
    persisted = _bridge_db.latest_cost_record(system="cc", month=month)
    if "error" in persisted:
        return persisted

    if not persisted.get("exists"):
        return {
            "month": month,
            "live_total_usd": live["total_usd"],
            "persisted_total_usd": None,
            "delta_usd": None,
            "delta_exceeds_threshold": None,
            "stale": True,
            "stale_reason": "missing_current_month_record",
            "persisted_recorded_at": None,
            "notes": "No bridge-db cost row exists for this month.",
        }

    delta = round(live["total_usd"] - persisted["amount_usd"], 6)
    delta_exceeds_threshold = abs(delta) >= 1.0
    return {
        "month": month,
        "live_total_usd": live["total_usd"],
        "persisted_total_usd": persisted["amount_usd"],
        "delta_usd": delta,
        "delta_exceeds_threshold": delta_exceeds_threshold,
        "stale": delta_exceeds_threshold,
        "stale_reason": "delta_exceeds_1_usd" if delta_exceeds_threshold else None,
        "persisted_recorded_at": persisted["recorded_at"],
        "notes": persisted["notes"],
    }


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

    Thresholds default to [100, 250, 500] USD. Override via
    ~/.config/cost-tracker/thresholds.toml with:
        thresholds_usd = [100, 250, 500, 1000]

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


@app.tool()
def cost_sync_sessions() -> dict[str, Any]:
    """
    Sync ccusage session data into bridge-db session_costs table.
    Maps sessions to projects via ~/.claude/projects/ directory structure.
    Run this to populate per-project cost attribution.
    Returns {synced, skipped, errors}.
    """
    from cost_tracker.session_sync import sync_session_costs

    return sync_session_costs()
