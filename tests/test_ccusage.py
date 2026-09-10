"""Tests for ccusage subprocess wrapper."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cost_tracker import ccusage

FIXTURES = Path(__file__).parent / "fixtures" / "ccusage_sample.json"
MALFORMED = Path(__file__).parent / "fixtures" / "ccusage_malformed.json"


class FixedDate(date):
    @classmethod
    def today(cls) -> FixedDate:
        return cls(2026, 5, 18)


@pytest.fixture()
def sample() -> dict:
    return json.loads(FIXTURES.read_text())


@pytest.fixture()
def malformed() -> dict:
    return json.loads(MALFORMED.read_text())


def _invoke(callback: Callable[[], Any], payload: object) -> Any:
    stdout = json.dumps(payload)
    with ExitStack() as stack:
        stack.enter_context(patch("cost_tracker.ccusage.date", FixedDate))
        stack.enter_context(patch("subprocess.run", return_value=_mock_run(stdout)))
        return callback()


def _unavailable_detail(result: object) -> str:
    if isinstance(result, list):
        assert result
        assert result[0]["error"] == "ccusage_unavailable"
        return result[0]["detail"]
    assert isinstance(result, dict)
    assert result["error"] == "ccusage_unavailable"
    return result["detail"]


CCUSAGE_JSON_ENTRYPOINTS: list[tuple[str, Callable[[], Any]]] = [
    ("cost_today", ccusage.cost_today),
    ("cost_session", ccusage.cost_session),
    ("cost_monthly_trend", lambda: ccusage.cost_monthly_trend(months=3)),
    ("cost_month_to_date", ccusage.cost_month_to_date),
    ("cost_top_days", lambda: ccusage.cost_top_days(days=14, limit=10)),
    ("cost_top_sessions", lambda: ccusage.cost_top_sessions(window_days=14, limit=10)),
]


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
        with (
            patch("cost_tracker.ccusage.date", FixedDate),
            patch("subprocess.run", return_value=_mock_run(payload)),
        ):
            result = ccusage.cost_today()

        assert result["date"] == FixedDate.today().isoformat()
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


class TestCostMonthToDate:
    def test_happy_path_returns_current_month(self, sample):
        payload = json.dumps(sample["monthly"])
        with (
            patch("cost_tracker.ccusage.date", FixedDate),
            patch("subprocess.run", return_value=_mock_run(payload)),
        ):
            result = ccusage.cost_month_to_date()

        assert result["month"] == "2026-05"
        assert result["total_usd"] == pytest.approx(75.0)
        assert result["by_model"]["opus"] == pytest.approx(75.0)

    def test_missing_current_month_returns_zero(self):
        payload = json.dumps({"monthly": []})
        with (
            patch("cost_tracker.ccusage.date", FixedDate),
            patch("subprocess.run", return_value=_mock_run(payload)),
        ):
            result = ccusage.cost_month_to_date()

        assert result["month"] == "2026-05"
        assert result["total_usd"] == 0.0
        assert result["by_model"] == {}


class TestCostTopDays:
    def test_sorts_days_by_cost_desc(self, sample):
        payload = json.dumps(
            {
                "daily": [
                    sample["daily_today"]["daily"][0],
                    {**sample["daily_today"]["daily"][0], "date": "2026-05-17", "totalCost": 2.0},
                ]
            }
        )
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_top_days(days=2, limit=2)

        assert [row["total_usd"] for row in result] == [12.5, 2.0]

    def test_nonpositive_limit_returns_empty_list(self, sample):
        payload = json.dumps(sample["daily_today"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_top_days(days=2, limit=0)

        assert result == []


class TestCostTopSessions:
    def test_returns_sorted_sessions_with_caveat(self, sample):
        payload = json.dumps(sample["session"])
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_top_sessions(window_days=14, limit=2)

        assert result["attribution"] == "workflow_signal_not_invoice_window"
        assert result["sessions"][0]["session_id"] == "-Users-d"
        assert result["sessions"][1]["session_id"] == "-Users-d-Projects-cost-tracker"

    def test_prefers_project_path_over_unknown_project(self, sample):
        payload = json.dumps(
            {
                "sessions": [
                    {
                        **sample["session"]["sessions"][0],
                        "project": "Unknown Project",
                        "projectPath": "~/Projects/cost-tracker",
                    }
                ]
            }
        )
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_top_sessions(window_days=14, limit=1)

        assert result["sessions"][0]["project"] == "~/Projects/cost-tracker"

    def test_falls_back_to_session_id_when_project_is_unknown(self, sample):
        payload = json.dumps(
            {
                "sessions": [
                    {
                        **sample["session"]["sessions"][0],
                        "project": "Unknown Project",
                        "projectPath": None,
                    }
                ]
            }
        )
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_top_sessions(window_days=14, limit=1)

        assert result["sessions"][0]["project"] == "-Users-d"

    def test_falls_back_to_session_id_when_project_path_is_unknown(self, sample):
        payload = json.dumps(
            {
                "sessions": [
                    {
                        **sample["session"]["sessions"][0],
                        "project": "Unknown Project",
                        "projectPath": "Unknown Project",
                    }
                ]
            }
        )
        with patch("subprocess.run", return_value=_mock_run(payload)):
            result = ccusage.cost_top_sessions(window_days=14, limit=1)

        assert result["sessions"][0]["project"] == "-Users-d"


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


class TestIterModelCosts:
    """ccusage modelBreakdowns parsing tolerates malformed payloads."""

    def test_null_returns_empty(self):
        assert ccusage._iter_model_costs(None) == []

    def test_non_list_returns_empty(self):
        assert ccusage._iter_model_costs({"modelName": "x"}) == []

    def test_skips_non_dict_entries(self):
        assert ccusage._iter_model_costs(["garbage", None, {"modelName": "m", "cost": 1.5}]) == [
            ("m", 1.5)
        ]

    def test_non_numeric_cost_becomes_zero(self):
        assert ccusage._iter_model_costs([{"modelName": "m", "cost": "oops"}]) == [("m", 0.0)]

    def test_missing_name_becomes_empty_string(self):
        assert ccusage._iter_model_costs([{"cost": 2.0}]) == [("", 2.0)]

    def test_extract_by_model_handles_null_breakdowns(self):
        assert ccusage._extract_by_model(None) == {}

    def test_extract_by_model_skips_non_dict_entries(self):
        result = ccusage._extract_by_model(
            ["garbage", {"modelName": "claude-opus-4-8", "cost": 2.0}]
        )
        assert result["opus"] == pytest.approx(2.0)
        assert len(result) == 1


class TestMalformedCcusageJson:
    """Every ccusage JSON entrypoint rejects wrong envelopes without inventing spend."""

    @pytest.mark.parametrize("_name, callback", CCUSAGE_JSON_ENTRYPOINTS)
    @pytest.mark.parametrize(
        "payload_key",
        [
            "top_level_array",
            "top_level_array_of_objects",
            "top_level_null",
            "top_level_number",
            "top_level_string",
        ],
    )
    def test_wrong_top_level_shape_is_unavailable(self, malformed, _name, callback, payload_key):
        result = _invoke(callback, malformed[payload_key])
        detail = _unavailable_detail(result)
        assert "unexpected JSON shape" in detail
        if isinstance(result, dict):
            assert "total_usd" not in result
            assert "current_usd" not in result
        else:
            assert "total_usd" not in result[0]

    def test_top_level_array_does_not_invent_daily_rows(self, malformed):
        result = _invoke(
            lambda: ccusage.cost_top_days(days=14, limit=10),
            malformed["top_level_array_of_objects"],
        )
        assert _unavailable_detail(result)
        assert isinstance(result, list)
        assert len(result) == 1

    @pytest.mark.parametrize(
        "payload_key, callback, empty_check",
        [
            ("daily_field_object", ccusage.cost_today, "today_zero"),
            ("daily_field_null", ccusage.cost_today, "today_zero"),
            ("daily_empty", ccusage.cost_today, "today_zero"),
            ("daily_field_object", lambda: ccusage.cost_top_days(days=14, limit=10), "empty_list"),
            ("daily_field_null", lambda: ccusage.cost_top_days(days=14, limit=10), "empty_list"),
            ("daily_empty", lambda: ccusage.cost_top_days(days=14, limit=10), "empty_list"),
            ("sessions_field_object", ccusage.cost_session, "session_zero"),
            ("sessions_field_null", ccusage.cost_session, "session_zero"),
            ("sessions_empty", ccusage.cost_session, "session_zero"),
            (
                "sessions_field_object",
                lambda: ccusage.cost_top_sessions(window_days=14, limit=10),
                "top_sessions_empty",
            ),
            (
                "sessions_field_null",
                lambda: ccusage.cost_top_sessions(window_days=14, limit=10),
                "top_sessions_empty",
            ),
            (
                "sessions_empty",
                lambda: ccusage.cost_top_sessions(window_days=14, limit=10),
                "top_sessions_empty",
            ),
            ("monthly_field_object", lambda: ccusage.cost_monthly_trend(months=3), "empty_list"),
            ("monthly_field_null", lambda: ccusage.cost_monthly_trend(months=3), "empty_list"),
            ("monthly_empty", lambda: ccusage.cost_monthly_trend(months=3), "empty_list"),
            ("monthly_field_object", ccusage.cost_month_to_date, "mtd_zero"),
            ("monthly_field_null", ccusage.cost_month_to_date, "mtd_zero"),
            ("monthly_empty", ccusage.cost_month_to_date, "mtd_zero"),
        ],
    )
    def test_empty_or_non_list_collections_use_stable_empty(
        self, malformed, payload_key, callback, empty_check
    ):
        result = _invoke(callback, malformed[payload_key])
        if empty_check == "today_zero":
            assert result == {
                "date": "2026-05-18",
                "total_usd": 0.0,
                "by_model": {},
                "session_count": 0,
            }
        elif empty_check == "session_zero":
            assert result == {
                "session_id": None,
                "started_at": "2026-05-18",
                "current_usd": 0.0,
                "by_model": {},
            }
        elif empty_check == "mtd_zero":
            assert result == {
                "month": "2026-05",
                "total_usd": 0.0,
                "total_tokens": 0,
                "by_model": {},
                "models_used": [],
            }
        elif empty_check == "empty_list":
            assert result == []
        else:
            assert result["attribution"] == "workflow_signal_not_invoice_window"
            assert result["sessions"] == []
            assert result["window_days"] == 14

    def test_mixed_daily_keeps_only_usable_rows(self, malformed):
        today = _invoke(ccusage.cost_today, malformed["daily_mixed"])
        assert today["total_usd"] == pytest.approx(12.5)
        assert today["by_model"]["opus"] == pytest.approx(12.5)
        assert today["session_count"] == 1

        days = _invoke(lambda: ccusage.cost_top_days(days=14, limit=10), malformed["daily_mixed"])
        assert [row["date"] for row in days] == ["2026-05-18", "2026-05-17"]
        assert [row["total_usd"] for row in days] == [12.5, 2.0]
        assert 99.0 not in [row["total_usd"] for row in days]
        assert 50 not in [row["total_usd"] for row in days]

    def test_mixed_sessions_keeps_last_usable_and_skips_bad_cost(self, malformed):
        current = _invoke(ccusage.cost_session, malformed["sessions_mixed"])
        assert current["session_id"] == "-Users-d-Projects-cost-tracker"
        assert current["current_usd"] == pytest.approx(3.25)

        top = _invoke(
            lambda: ccusage.cost_top_sessions(window_days=14, limit=10),
            malformed["sessions_mixed"],
        )
        assert top["attribution"] == "workflow_signal_not_invoice_window"
        assert [row["session_id"] for row in top["sessions"]] == [
            "-Users-d",
            "-Users-d-Projects-cost-tracker",
        ]
        assert "bad-cost" not in [row["session_id"] for row in top["sessions"]]

    def test_mixed_monthly_keeps_only_usable_months(self, malformed):
        trend = _invoke(lambda: ccusage.cost_monthly_trend(months=3), malformed["monthly_mixed"])
        assert [row["month"] for row in trend] == ["2026-04", "2026-05"]
        assert trend[1]["total_usd"] == pytest.approx(75.0)
        assert 999 not in [row["total_usd"] for row in trend]
        assert 12.0 not in [row["total_usd"] for row in trend]

        mtd = _invoke(ccusage.cost_month_to_date, malformed["monthly_mixed"])
        assert mtd["month"] == "2026-05"
        assert mtd["total_usd"] == pytest.approx(75.0)
        assert mtd["by_model"]["opus"] == pytest.approx(75.0)

    def test_missing_and_wrong_typed_nested_fields_do_not_invent_usage(self, malformed):
        today = _invoke(ccusage.cost_today, malformed["daily_missing_and_wrong_types"])
        assert today["total_usd"] == pytest.approx(4.5)
        assert today["by_model"] == {}
        assert today["session_count"] == 0

        days = _invoke(
            lambda: ccusage.cost_top_days(days=14, limit=10),
            malformed["daily_missing_and_wrong_types"],
        )
        assert [row["date"] for row in days] == ["2026-05-18"]
        assert days[0]["total_tokens"] == 0
        assert days[0]["models_used"] == []

        session = _invoke(ccusage.cost_session, malformed["sessions_missing_and_wrong_types"])
        assert session["session_id"] == "-Users-d"
        assert session["current_usd"] == 0.0
        assert session["by_model"] == {}

        top = _invoke(
            lambda: ccusage.cost_top_sessions(window_days=14, limit=10),
            malformed["sessions_missing_and_wrong_types"],
        )
        assert top["sessions"][0]["total_usd"] == 0.0
        assert top["sessions"][0]["total_tokens"] == 0
        assert top["sessions"][0]["models_used"] == []

        mtd = _invoke(ccusage.cost_month_to_date, malformed["monthly_missing_and_wrong_types"])
        assert mtd["month"] == "2026-05"
        assert mtd["total_usd"] == 0.0
        assert mtd["total_tokens"] == 0
        assert mtd["models_used"] == []
        assert mtd["by_model"] == {}

    @pytest.mark.parametrize("_name, callback", CCUSAGE_JSON_ENTRYPOINTS)
    def test_unavailable_cli_still_uses_stable_error(self, _name, callback):
        with ExitStack() as stack:
            stack.enter_context(patch("cost_tracker.ccusage.date", FixedDate))
            stack.enter_context(patch("subprocess.run", side_effect=FileNotFoundError))
            result = callback()
        detail = _unavailable_detail(result)
        assert "PATH" in detail


class TestObjectEntries:
    def test_null_and_object_return_empty(self):
        assert ccusage._object_entries(None) == []
        assert ccusage._object_entries({"sessionId": "x"}) == []

    def test_skips_non_dict_entries(self):
        assert ccusage._object_entries([None, "x", {"a": 1}, 3]) == [{"a": 1}]
