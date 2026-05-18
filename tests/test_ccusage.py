"""Tests for ccusage subprocess wrapper."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cost_tracker import ccusage

FIXTURES = Path(__file__).parent / "fixtures" / "ccusage_sample.json"


@pytest.fixture()
def sample() -> dict:
    return json.loads(FIXTURES.read_text())


def _mock_run(stdout: str):
    """Return a mock for subprocess.run that produces stdout."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


class TestCostToday:
    def test_happy_path_returns_today_entry(self, sample):
        payload = json.dumps(sample["daily_today"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_today()

        assert result["date"] == date.today().isoformat()
        assert result["total_usd"] == 12.5
        assert result["by_model"]["opus"] == pytest.approx(9.5)
        assert result["by_model"]["sonnet"] == pytest.approx(2.75)
        assert result["by_model"]["haiku"] == pytest.approx(0.25)

    def test_empty_daily_returns_zero(self, sample):
        payload = json.dumps(sample["daily_empty"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_today()

        assert result["total_usd"] == 0.0
        assert result["by_model"] == {}
        assert result["session_count"] == 0

    def test_binary_not_found_returns_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = ccusage.cost_today()

        assert result["error"] == "ccusage_unavailable"
        assert "PATH" in result["detail"]

    def test_nonzero_exit_returns_error(self):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "fatal error"
        with patch("subprocess.run", return_value=m):
            result = ccusage.cost_today()

        assert result["error"] == "ccusage_unavailable"
        assert "fatal error" in result["detail"]

    def test_bad_json_returns_error(self):
        with patch("subprocess.run", return_value=_mock_run("not-json")):
            result = ccusage.cost_today()

        assert result["error"] == "ccusage_unavailable"
        assert "JSON" in result["detail"]


class TestCostSession:
    def test_happy_path_returns_last_session(self, sample):
        payload = json.dumps(sample["session"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_session()

        # Returns the last (most-recent) session
        assert result["session_id"] == "-Users-d-Projects-cost-tracker"
        assert result["current_usd"] == pytest.approx(3.25)
        assert result["by_model"]["sonnet"] == pytest.approx(3.25)

    def test_empty_sessions_returns_zero(self):
        payload = json.dumps({"sessions": []})
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_session()

        assert result["current_usd"] == 0.0
        assert result["session_id"] is None

    def test_binary_missing_returns_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = ccusage.cost_session()

        assert result["error"] == "ccusage_unavailable"


class TestCostMonthlyTrend:
    def test_happy_path_sorted_oldest_first(self, sample):
        payload = json.dumps(sample["monthly"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_monthly_trend(months=3)

        assert len(result) == 3
        assert result[0]["month"] == "2026-03"
        assert result[1]["month"] == "2026-04"
        assert result[2]["month"] == "2026-05"

    def test_model_breakdown_aggregated(self, sample):
        payload = json.dumps(sample["monthly"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_monthly_trend(months=3)

        apr = result[1]
        assert apr["by_model"]["opus"] == pytest.approx(240.0)
        assert apr["by_model"]["sonnet"] == pytest.approx(55.0)
        assert apr["by_model"]["haiku"] == pytest.approx(5.0)

    def test_failure_returns_error_list(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = ccusage.cost_monthly_trend(months=3)

        assert isinstance(result, list)
        assert result[0]["error"] == "ccusage_unavailable"


class TestModelFamily:
    @pytest.mark.parametrize(
        "model, expected",
        [
            ("claude-opus-4-7", "opus"),
            ("claude-opus-4-6", "opus"),
            ("claude-sonnet-4-6", "sonnet"),
            ("claude-haiku-4-5-20251001", "haiku"),
            ("claude-unknown-model", "other"),
        ],
    )
    def test_family_mapping(self, model, expected):
        assert ccusage._model_family(model) == expected
