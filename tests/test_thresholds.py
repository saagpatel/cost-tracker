"""Tests for threshold check logic."""

from __future__ import annotations

import pytest

from cost_tracker.thresholds import check_thresholds, load_thresholds


class TestCheckThresholds:
    @pytest.mark.parametrize(
        "today_usd, thresholds, expected_crossed, expected_next",
        [
            # Below all thresholds
            (2.0, [5.0, 15.0, 30.0], [], 5.0),
            # Exactly at first threshold
            (5.0, [5.0, 15.0, 30.0], [5.0], 15.0),
            # Between first and second
            (8.0, [5.0, 15.0, 30.0], [5.0], 15.0),
            # Exactly at second threshold
            (15.0, [5.0, 15.0, 30.0], [5.0, 15.0], 30.0),
            # Above all thresholds
            (35.0, [5.0, 15.0, 30.0], [5.0, 15.0, 30.0], None),
            # Zero spend
            (0.0, [5.0, 15.0, 30.0], [], 5.0),
            # Single threshold
            (10.0, [20.0], [], 20.0),
            # Single threshold crossed
            (25.0, [20.0], [20.0], None),
            # Custom 4-tier thresholds
            (16.0, [5.0, 15.0, 30.0, 50.0], [5.0, 15.0], 30.0),
        ],
    )
    def test_parametrized(self, today_usd, thresholds, expected_crossed, expected_next):
        result = check_thresholds(today_usd, thresholds)

        assert result["today_usd"] == pytest.approx(today_usd)
        assert result["thresholds_crossed"] == expected_crossed
        assert result["next_threshold"] == expected_next

    def test_headroom_computed_correctly(self):
        result = check_thresholds(8.0, [5.0, 15.0, 30.0])

        assert result["headroom_usd"] == pytest.approx(7.0)

    def test_headroom_none_when_all_crossed(self):
        result = check_thresholds(100.0, [5.0, 15.0, 30.0])

        assert result["headroom_usd"] is None
        assert result["next_threshold"] is None

    def test_thresholds_need_not_be_sorted_in_input(self):
        result = check_thresholds(8.0, [30.0, 5.0, 15.0])

        assert result["thresholds_crossed"] == [5.0]
        assert result["next_threshold"] == 15.0


class TestLoadThresholds:
    def test_defaults_when_no_config(self, tmp_path):
        result = load_thresholds(config_path=tmp_path / "nonexistent.toml")

        assert result == [100.0, 250.0, 500.0]

    def test_loads_from_toml(self, tmp_path):
        cfg = tmp_path / "thresholds.toml"
        cfg.write_text("thresholds_usd = [10, 25, 50, 100]\n")

        result = load_thresholds(config_path=cfg)

        assert result == [10.0, 25.0, 50.0, 100.0]

    def test_bad_toml_falls_back_to_defaults(self, tmp_path):
        cfg = tmp_path / "thresholds.toml"
        cfg.write_bytes(b"\xff\xfe invalid bytes")

        result = load_thresholds(config_path=cfg)

        assert result == [100.0, 250.0, 500.0]

    def test_toml_values_are_sorted(self, tmp_path):
        cfg = tmp_path / "thresholds.toml"
        cfg.write_text("thresholds_usd = [50, 5, 20]\n")

        result = load_thresholds(config_path=cfg)

        assert result == [5.0, 20.0, 50.0]
