# cost-tracker

`cost-tracker` is a local Python 3.12 MCP server for live in-session cost
visibility. It is intentionally small: parse local usage data, apply threshold
logic, optionally read bridge-db context, and expose the result over stdio via
the `cost-tracker` entrypoint.

## Current State

Current portfolio truth should be checked in
`/Users/d/Projects/GithubRepoAuditor/output/portfolio-truth-latest.json`; recent
runs mark this repo as `active-infra`. The repo still has minimum-viable
context, so treat `AGENTS.md`, `pyproject.toml`, and the live source tree as the
restart authority until a deeper roadmap or handoff exists.

## Setup

```bash
uv sync --group dev
```

## Run

```bash
uv run cost-tracker
```

## MCP Tools

Read-only visibility tools:

- `cost_today` - today's Claude Code spend from live `ccusage`
- `cost_session` - most-recent active session/workflow spend
- `cost_monthly_trend` - recent monthly Claude Code trend
- `cost_month_to_date` - current month-to-date Claude Code actuals
- `cost_top_days` - highest-spend recent days
- `cost_top_sessions` - high-cost workflow/session groups with attribution caveats
- `cost_bridge_staleness` - live-vs-persisted bridge-db delta check
- `cost_top_projects` - bridge-db cost records grouped by project
- `cost_alert_thresholds_check` - today's spend against local thresholds

Write-capable manual tool:

- `cost_record` - inserts one cost row into bridge-db `cost_records`

`cost-oracle` remains the report-only monthly automation under
`/Users/d/.codex/automations/cost-oracle/automation.toml`. This MCP server is a
local visibility/helper surface, not the scheduled cost-report authority.

## Verify

```bash
uv run pytest
uv run ruff check .
```

## Key Files

- `src/cost_tracker/server.py` - MCP server surface
- `src/cost_tracker/ccusage.py` - usage parsing
- `src/cost_tracker/thresholds.py` - threshold logic
- `src/cost_tracker/bridge_db.py` - bridge-db integration helper

Keep changes local, inspectable, and focused on cost visibility.
