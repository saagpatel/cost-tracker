"""Tests for MCP server wrapper logic."""

from __future__ import annotations

import pytest

from cost_tracker import server


class TestCostBridgeStaleness:
    def test_live_ccusage_error_propagates(self, monkeypatch):
        monkeypatch.setattr(
            server._ccusage,
            "cost_month_to_date",
            lambda: {"error": "ccusage_unavailable", "detail": "missing"},
        )

        result = server.cost_bridge_staleness()

        assert result == {"error": "ccusage_unavailable", "detail": "missing"}

    def test_bridge_error_propagates(self, monkeypatch):
        monkeypatch.setattr(
            server._ccusage,
            "cost_month_to_date",
            lambda: {"month": "2026-06", "total_usd": 10.0},
        )
        monkeypatch.setattr(
            server._bridge_db,
            "latest_cost_record",
            lambda system, month: {"error": "bridge_db_error", "detail": "locked"},
        )

        result = server.cost_bridge_staleness()

        assert result == {"error": "bridge_db_error", "detail": "locked"}

    def test_missing_persisted_row_reports_stale_reason(self, monkeypatch):
        monkeypatch.setattr(
            server._ccusage,
            "cost_month_to_date",
            lambda: {"month": "2026-06", "total_usd": 10.0},
        )
        monkeypatch.setattr(
            server._bridge_db,
            "latest_cost_record",
            lambda system, month: {"system": system, "month": month, "exists": False},
        )

        result = server.cost_bridge_staleness()

        assert result["stale"] is True
        assert result["stale_reason"] == "missing_current_month_record"
        assert result["delta_exceeds_threshold"] is None
        assert result["persisted_total_usd"] is None

    @pytest.mark.parametrize(
        "persisted_total, expected_delta, expected_exceeds, expected_reason",
        [
            (9.5, 0.5, False, None),
            (8.0, 2.0, True, "delta_exceeds_1_usd"),
        ],
    )
    def test_existing_row_reports_delta_state(
        self,
        monkeypatch,
        persisted_total,
        expected_delta,
        expected_exceeds,
        expected_reason,
    ):
        monkeypatch.setattr(
            server._ccusage,
            "cost_month_to_date",
            lambda: {"month": "2026-06", "total_usd": 10.0},
        )
        monkeypatch.setattr(
            server._bridge_db,
            "latest_cost_record",
            lambda system, month: {
                "system": system,
                "month": month,
                "exists": True,
                "amount_usd": persisted_total,
                "recorded_at": "2026-06-14T00:00:00Z",
                "notes": "test row",
            },
        )

        result = server.cost_bridge_staleness()

        assert result["delta_usd"] == pytest.approx(expected_delta)
        assert result["delta_exceeds_threshold"] is expected_exceeds
        assert result["stale"] is expected_exceeds
        assert result["stale_reason"] == expected_reason
