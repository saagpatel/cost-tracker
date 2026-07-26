"""Tests for invoice calibration of the 1h cache-write multiplier."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from cost_tracker.__main__ import _build_parser, _parse_month_spec
from cost_tracker.calibrate import (
    TokenTotals,
    calibrate,
    format_report,
    month_token_totals,
    total_under,
)

# One million 1h-cache-write tokens on Opus 5 ($5/MTok input): $10.00 at the 2.0
# multiplier, $6.25 at 1.25. A clean separation to fit against.
OPUS_INPUT_RATE = 5.0


def _entry(message_id: str, *, day: str, cache_1h: int = 0, output: int = 0) -> dict:
    return {
        "type": "assistant",
        "timestamp": f"{day}T12:00:00.000Z",
        "cwd": "/workspace/Projects/demo",
        "isSidechain": False,
        "sessionId": "s1",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 0,
                "output_tokens": output,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": cache_1h,
                    "ephemeral_5m_input_tokens": 0,
                },
            },
        },
    }


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    (root / "-Users-d-Projects-demo").mkdir(parents=True)
    return root


def _write(root: Path, name: str, entries: list[dict]) -> None:
    path = root / "-Users-d-Projects-demo" / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


class TestTotalUnder:
    def test_one_hour_multiplier_changes_the_total(self):
        totals = {"claude-opus-5": TokenTotals(cache_write_1h_tokens=1_000_000)}
        assert total_under(totals, cache_write_1h_multiplier=2.0) == pytest.approx(10.0)
        assert total_under(totals, cache_write_1h_multiplier=1.25) == pytest.approx(6.25)

    def test_terms_other_than_1h_are_unaffected(self):
        totals = {"claude-opus-5": TokenTotals(output_tokens=1_000_000)}
        assert total_under(totals, cache_write_1h_multiplier=2.0) == pytest.approx(25.0)
        assert total_under(totals, cache_write_1h_multiplier=1.25) == pytest.approx(25.0)

    def test_unpriced_models_contribute_nothing(self):
        totals = {"claude-nextgen-9": TokenTotals(output_tokens=1_000_000)}
        assert total_under(totals, cache_write_1h_multiplier=2.0) == 0.0


class TestMonthTokenTotals:
    def test_buckets_by_month_and_model(self, corpus):
        _write(
            corpus,
            "s1",
            [
                _entry("a", day="2026-06-15", cache_1h=1_000_000),
                _entry("b", day="2026-07-15", cache_1h=2_000_000),
            ],
        )
        totals = month_token_totals({"2026-06", "2026-07"}, corpus)
        assert totals["2026-06"]["claude-opus-5"].cache_write_1h_tokens == 1_000_000
        assert totals["2026-07"]["claude-opus-5"].cache_write_1h_tokens == 2_000_000

    def test_months_not_requested_are_ignored(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-05-01", cache_1h=999)])
        assert month_token_totals({"2026-06"}, corpus) == {"2026-06": {}}

    def test_one_scan_serves_every_candidate(self, corpus):
        """Totals are reduced once; candidates are then pure arithmetic."""
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        totals = month_token_totals({"2026-06"}, corpus)["2026-06"]
        assert total_under(totals, cache_write_1h_multiplier=2.0) == pytest.approx(10.0)
        assert total_under(totals, cache_write_1h_multiplier=1.25) == pytest.approx(6.25)


class TestCalibrate:
    def test_picks_the_multiplier_matching_the_invoice(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 10.0}, corpus)
        assert report["best"]["cache_write_1h_multiplier"] == 2.0
        assert report["best"]["within_tolerance"] is True

    def test_picks_the_other_multiplier_when_that_is_what_fits(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 6.25}, corpus)
        assert report["best"]["cache_write_1h_multiplier"] == 1.25
        assert report["best"]["within_tolerance"] is True

    def test_hypotheses_ranked_by_absolute_error(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 10.0}, corpus)
        errors = [h["abs_error_usd"] for h in report["hypotheses"]]
        assert errors == sorted(errors)

    def test_multiple_months_are_fitted_together(self, corpus):
        _write(
            corpus,
            "s1",
            [
                _entry("a", day="2026-06-15", cache_1h=1_000_000),
                _entry("b", day="2026-07-15", cache_1h=1_000_000),
            ],
        )
        report = calibrate({"2026-06": 10.0, "2026-07": 10.0}, corpus)
        assert report["months"] == ["2026-06", "2026-07"]
        assert report["best"]["cache_write_1h_multiplier"] == 2.0

    def test_custom_candidates_are_honoured(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 15.0}, corpus, candidates=(1.25, 2.0, 3.0))
        assert report["best"]["cache_write_1h_multiplier"] == 3.0

    def test_no_observations_is_an_error(self, corpus):
        assert calibrate({}, corpus)["error"] == "no_observations"

    def test_month_with_no_transcripts_is_reported_not_fitted(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 10.0, "2026-01": 500.0}, corpus)
        assert report["months"] == ["2026-06"]
        assert any("2026-01" in w for w in report["warnings"])

    def test_all_months_empty_refuses_to_fit(self, corpus):
        report = calibrate({"2026-01": 500.0}, corpus)
        assert report["error"] == "no_usable_months"


class TestCalibrationRefusesWhenTheInvoiceCannotSettleIt:
    """The two conditions that make an invoice unusable must be caught.

    Both produce a plausible-looking ranking, so silence here would mean adopting
    a multiplier fitted to the wrong quantity.
    """

    def test_universal_underestimate_flags_untracked_usage(self, corpus):
        """Transcripts cover Claude Code only, so the estimate is a floor."""
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 5_000.0}, corpus)
        assert any("underestimate" in w for w in report["warnings"])
        assert report["best"]["within_tolerance"] is False

    def test_universal_overestimate_flags_subscription_pricing(self, corpus):
        """A plan price cannot calibrate a token estimator."""
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 0.50}, corpus)
        assert any("subscription" in w for w in report["warnings"])
        assert report["best"]["within_tolerance"] is False

    def test_best_fit_outside_tolerance_is_flagged(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        report = calibrate({"2026-06": 12.0}, corpus)
        assert any("tolerance" in w for w in report["warnings"])

    def test_clean_fit_produces_no_warnings(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        assert calibrate({"2026-06": 10.0}, corpus)["warnings"] == []


class TestFormatReport:
    def test_renders_every_candidate_and_the_verdict(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        text = format_report(calibrate({"2026-06": 10.0}, corpus))
        assert "1.25" in text and "2.00" in text
        assert "best fit" in text and "within tolerance" in text

    def test_surfaces_warnings(self, corpus):
        _write(corpus, "s1", [_entry("a", day="2026-06-15", cache_1h=1_000_000)])
        text = format_report(calibrate({"2026-06": 5_000.0}, corpus))
        assert "WARNING" in text

    def test_renders_errors(self, corpus):
        assert "calibration failed" in format_report(calibrate({}, corpus))


class TestCli:
    def test_month_spec_parses(self):
        assert _parse_month_spec("2026-06=1234.56") == ("2026-06", 1234.56)

    def test_month_spec_tolerates_currency_formatting(self):
        """A total pasted straight from a console carries $ and thousands commas."""
        assert _parse_month_spec("2026-06=$1,234.56") == ("2026-06", 1234.56)

    @pytest.mark.parametrize("bad", ["2026-06", "june=100", "2026-6=100", "2026-06=abc"])
    def test_month_spec_rejects_malformed_input(self, bad):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_month_spec(bad)

    def test_bare_invocation_selects_the_server(self):
        """A stray subcommand default would corrupt MCP's stdout JSON-RPC."""
        assert _build_parser().parse_args([]).command is None

    def test_calibrate_subcommand_collects_months(self):
        args = _build_parser().parse_args(
            ["calibrate", "--month", "2026-06=100", "--month", "2026-07=200"]
        )
        assert args.command == "calibrate"
        assert dict(args.month) == {"2026-06": 100.0, "2026-07": 200.0}

    def test_calibrate_requires_at_least_one_month(self):
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["calibrate"])
