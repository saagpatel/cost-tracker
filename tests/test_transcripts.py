"""Tests for transcript-derived per-project, per-day cost attribution."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from cost_tracker.transcripts import (
    BASE_RATES,
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    compute_cost,
    iter_usage_records,
    project_daily_costs,
    project_from_cwd,
    project_window_costs,
    resolve_rate,
    summarise_unpriced,
)


def _usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_1h: int = 0,
    cache_5m: int = 0,
    legacy_cache_creation: int | None = None,
) -> dict:
    usage: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
    }
    if legacy_cache_creation is not None:
        usage["cache_creation_input_tokens"] = legacy_cache_creation
    else:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cache_1h,
            "ephemeral_5m_input_tokens": cache_5m,
        }
    return usage


def _entry(
    message_id: str,
    *,
    day: str = "2026-07-20",
    model: str = "claude-opus-5",
    cwd: str = "/workspace/Projects/demo",
    usage: dict | None = None,
    sidechain: bool = False,
    session_id: str = "sess-1",
) -> dict:
    return {
        "type": "assistant",
        "timestamp": f"{day}T12:00:00.000Z",
        "cwd": cwd,
        "isSidechain": sidechain,
        "sessionId": session_id,
        "message": {
            "id": message_id,
            "model": model,
            "usage": usage if usage is not None else _usage(output_tokens=1_000_000),
        },
    }


@pytest.fixture()
def transcripts_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


def _write(root: Path, dirname: str, session: str, entries: list[dict]) -> Path:
    subdir = root / dirname
    subdir.mkdir(exist_ok=True)
    path = subdir / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


class TestResolveRate:
    def test_exact_model_id(self):
        assert resolve_rate("claude-opus-5") == (5.0, 25.0)

    def test_dated_suffix_resolves_to_family(self):
        """Model ids carry date suffixes in real transcripts."""
        assert resolve_rate("claude-haiku-4-5-20251001") == (1.0, 5.0)

    def test_unknown_model_returns_none_not_zero(self):
        """An unpriced model must be a visible gap, never a silent zero."""
        assert resolve_rate("claude-nextgen-9") is None
        assert resolve_rate(None) is None

    def test_every_rate_is_positive(self):
        for model, (inp, out) in BASE_RATES.items():
            assert inp > 0 and out > 0, model

    def test_documented_cache_multipliers_are_not_silently_changed(self):
        """Pin Anthropic's published cache multipliers.

        The 1h multiplier is the one someone is most likely to "fix" downward to
        make totals agree with ccusage. ccusage prices all cache creation at the
        5m rate, which under-prices 1h writes to 0.625x of the billed amount;
        matching it would understate a real monthly total by roughly 14%.
        """
        assert CACHE_READ_MULTIPLIER == 0.1
        assert CACHE_WRITE_5M_MULTIPLIER == 1.25
        assert CACHE_WRITE_1H_MULTIPLIER == 2.0


class TestComputeCost:
    def test_output_tokens_bill_at_output_rate(self):
        cost = compute_cost("claude-opus-5", _usage(output_tokens=1_000_000))
        assert cost == pytest.approx(25.0)

    def test_cache_read_bills_at_one_tenth_input(self):
        cost = compute_cost("claude-opus-5", _usage(cache_read=1_000_000))
        assert cost == pytest.approx(0.5)

    def test_one_hour_cache_write_bills_at_double_input(self):
        """The 1h/5m split dominates real spend — 1h writes outnumber 5m ~300:1."""
        cost = compute_cost("claude-opus-5", _usage(cache_1h=1_000_000))
        assert cost == pytest.approx(10.0)

    def test_five_minute_cache_write_bills_at_1_25x_input(self):
        cost = compute_cost("claude-opus-5", _usage(cache_5m=1_000_000))
        assert cost == pytest.approx(6.25)

    def test_one_hour_and_five_minute_are_not_conflated(self):
        """Costing 1h at the 5m rate understates by 37.5% on the dominant term."""
        hour = compute_cost("claude-opus-5", _usage(cache_1h=1_000_000))
        five = compute_cost("claude-opus-5", _usage(cache_5m=1_000_000))
        assert hour > five

    def test_legacy_transcripts_without_split_use_five_minute_rate(self):
        cost = compute_cost("claude-opus-5", _usage(legacy_cache_creation=1_000_000))
        assert cost == pytest.approx(6.25)

    def test_unpriced_model_returns_none(self):
        assert compute_cost("claude-nextgen-9", _usage(output_tokens=1_000_000)) is None


class TestProjectFromCwd:
    def test_projects_directory(self):
        assert project_from_cwd("/workspace/Projects/cost-tracker", home=Path("/workspace")) == (
            "cost-tracker"
        )

    def test_nested_path_resolves_to_the_project_root(self):
        assert project_from_cwd("/workspace/Projects/ink/src/lib", home=Path("/workspace")) == "ink"

    def test_dashed_and_spaced_names_survive_exactly(self):
        """The cwd path is real, so no lossy dash-decoding is needed."""
        assert project_from_cwd(
            "/workspace/Projects/Devil's Advocate", home=Path("/workspace")
        ) == ("Devil's Advocate")

    def test_local_share_service(self):
        assert project_from_cwd(
            "/workspace/.local/share/personal-ops", home=Path("/workspace")
        ) == ("personal-ops")

    def test_bare_home_is_the_adhoc_bucket(self):
        assert project_from_cwd("/workspace", home=Path("/workspace")) == "home-adhoc"

    def test_temp_sandboxes_bucket_as_transient(self):
        assert project_from_cwd("/private/var/folders/gf/T/frontier-s1") == "(transient)"

    def test_missing_cwd_returns_none(self):
        assert project_from_cwd(None) is None


class TestWorktreeAttribution:
    """Worktrees are per-project checkouts, not projects.

    Unresolved, every project's parallel-agent spend collapses into one bucket
    named after the container directory.
    """

    @pytest.fixture()
    def home(self, tmp_path: Path) -> Path:
        (tmp_path / "Projects" / "portfolio-index").mkdir(parents=True)
        (tmp_path / "Projects" / "portfolio").mkdir()
        (tmp_path / ".local" / "share" / "personal-ops").mkdir(parents=True)
        return tmp_path

    def test_worktree_resolves_to_its_project(self, home):
        cwd = str(home / "Projects" / "_claude-worktrees" / "portfolio-index-forge")
        assert project_from_cwd(cwd, home=home) == "portfolio-index"

    def test_longest_matching_project_wins(self, home):
        """`portfolio` also matches, but `portfolio-index` is the real checkout."""
        cwd = str(home / "Projects" / "_claude-worktrees" / "portfolio-index-forge")
        assert project_from_cwd(cwd, home=home) == "portfolio-index"

    def test_fable_worktrees_container_is_also_resolved(self, home):
        cwd = str(home / "Projects" / "_fable-worktrees" / "personal-ops-worklist-phase1")
        assert project_from_cwd(cwd, home=home) == "personal-ops"

    def test_nested_path_inside_a_worktree_still_resolves(self, home):
        cwd = str(home / "Projects" / "_fable-worktrees" / "personal-ops-merge" / "app" / "src")
        assert project_from_cwd(cwd, home=home) == "personal-ops"

    def test_unmatched_worktree_keeps_its_own_name(self, home):
        """A new project must stay visible, not inherit another's spend."""
        cwd = str(home / "Projects" / "_claude-worktrees" / "brand-new-thing")
        assert project_from_cwd(cwd, home=home) == "brand-new-thing"

    def test_container_itself_is_not_a_project(self, home):
        cwd = str(home / "Projects" / "_claude-worktrees")
        assert project_from_cwd(cwd, home=home) is None

    @pytest.mark.parametrize(
        "container", ["_claude-worktrees", "_fable-worktrees", "_codex-worktrees", ".worktrees"]
    )
    def test_every_observed_container_layout_is_unwrapped(self, home, container):
        cwd = str(home / "Projects" / container / "portfolio-index-somebranch")
        assert project_from_cwd(cwd, home=home) == "portfolio-index"

    def test_project_named_like_a_worktree_is_not_treated_as_a_container(self, home):
        """`worktree-manager` is a project; only `_`/`.` prefixes are containers."""
        (home / "Projects" / "worktree-manager").mkdir()
        cwd = str(home / "Projects" / "worktree-manager" / "src")
        assert project_from_cwd(cwd, home=home) == "worktree-manager"

    def test_worktree_nested_inside_a_project_keeps_that_project(self, home):
        """The in-project layout needs no unwrapping — the project is already first."""
        cwd = str(home / "Projects" / "portfolio-index" / ".claude" / "worktrees" / "feat-a11y")
        assert project_from_cwd(cwd, home=home) == "portfolio-index"


class TestIterUsageRecords:
    def test_deduplicates_messages_repeated_across_files(self, transcripts_dir):
        """Resumed/forked sessions re-serialize earlier messages into new files.

        On a real corpus ~60% of usage lines are repeats; summing without
        deduplicating overstates spend by roughly 2.5x.
        """
        entry = _entry("msg-1")
        _write(transcripts_dir, "-Users-d-Projects-demo", "sess-a", [entry])
        _write(transcripts_dir, "-Users-d-Projects-demo", "sess-b", [entry])

        records = list(iter_usage_records(transcripts_dir))
        assert len(records) == 1
        assert records[0].cost_usd == pytest.approx(25.0)

    def test_attributes_each_message_to_its_own_day(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("m1", day="2026-07-01"), _entry("m2", day="2026-07-20")],
        )
        days = sorted(r.day for r in iter_usage_records(transcripts_dir))
        assert days == ["2026-07-01", "2026-07-20"]

    def test_since_and_until_filter_by_message_day(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [
                _entry("m1", day="2026-07-01"),
                _entry("m2", day="2026-07-15"),
                _entry("m3", day="2026-07-30"),
            ],
        )
        records = list(iter_usage_records(transcripts_dir, since="2026-07-10", until="2026-07-20"))
        assert [r.day for r in records] == ["2026-07-15"]

    def test_two_projects_are_attributed_separately(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-alpha",
            "sess-a",
            [_entry("m1", cwd="/workspace/Projects/alpha")],
        )
        _write(
            transcripts_dir,
            "-Users-d-Projects-beta",
            "sess-b",
            [_entry("m2", cwd="/workspace/Projects/beta")],
        )
        assert {r.project for r in iter_usage_records(transcripts_dir)} == {"alpha", "beta"}

    def test_subagent_messages_are_flagged(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("m1", sidechain=True), _entry("m2", sidechain=False)],
        )
        flags = sorted(r.is_subagent for r in iter_usage_records(transcripts_dir))
        assert flags == [False, True]

    def test_unpriced_messages_are_excluded_not_costed_at_zero(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("m1", model="claude-nextgen-9"), _entry("m2")],
        )
        records = list(iter_usage_records(transcripts_dir))
        assert [r.model for r in records] == ["claude-opus-5"]

    def test_malformed_lines_do_not_abort_the_scan(self, transcripts_dir):
        subdir = transcripts_dir / "-Users-d-Projects-demo"
        subdir.mkdir()
        (subdir / "sess-a.jsonl").write_text(
            '{"usage": broken json\n' + json.dumps(_entry("m1")) + "\n", encoding="utf-8"
        )
        assert len(list(iter_usage_records(transcripts_dir))) == 1

    def test_missing_directory_yields_nothing(self, tmp_path):
        assert list(iter_usage_records(tmp_path / "absent")) == []

    def test_nested_subagent_transcripts_are_included(self, transcripts_dir):
        """Subagent transcripts sit a level deeper and are most of a real corpus.

        A one-level glob drops every one of them. That shipped once: it removed
        tens of thousands of billed messages while the grand total still looked
        right, because the omission cancelled an overcount elsewhere.
        """
        subagent_dir = transcripts_dir / "-Users-d-Projects-demo" / "parent-uuid" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-abc123.jsonl").write_text(
            json.dumps(_entry("sub-1", sidechain=True)) + "\n", encoding="utf-8"
        )
        _write(transcripts_dir, "-Users-d-Projects-demo", "sess-a", [_entry("lead-1")])

        records = list(iter_usage_records(transcripts_dir))
        assert sorted(r.is_subagent for r in records) == [False, True]

    def test_subagent_messages_are_not_duplicates_of_the_parent(self, transcripts_dir):
        """Recursion must add real spend, never double-count the parent's."""
        subagent_dir = transcripts_dir / "-Users-d-Projects-demo" / "parent-uuid" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-abc123.jsonl").write_text(
            json.dumps(_entry("sub-1", sidechain=True)) + "\n", encoding="utf-8"
        )
        _write(transcripts_dir, "-Users-d-Projects-demo", "sess-a", [_entry("lead-1")])

        total = sum(r.cost_usd for r in iter_usage_records(transcripts_dir))
        assert total == pytest.approx(50.0)

    def test_unpriced_scan_covers_nested_files_too(self, transcripts_dir):
        """Both scanners share one walk, so their coverage cannot drift apart."""
        subagent_dir = transcripts_dir / "-Users-d-Projects-demo" / "parent-uuid" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-abc123.jsonl").write_text(
            json.dumps(_entry("sub-1", model="claude-nextgen-9", sidechain=True)) + "\n",
            encoding="utf-8",
        )
        assert summarise_unpriced(transcripts_dir) == {"claude-nextgen-9": 1}


class TestSummariseUnpriced:
    def test_counts_models_without_a_rate(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("m1", model="claude-nextgen-9"), _entry("m2", model="claude-opus-5")],
        )
        assert summarise_unpriced(transcripts_dir) == {"claude-nextgen-9": 1}

    def test_synthetic_messages_are_not_reported_as_a_pricing_gap(self, transcripts_dir):
        """`<synthetic>` messages are locally generated and were never billed."""
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("m1", model="<synthetic>")],
        )
        assert summarise_unpriced(transcripts_dir) == {}

    def test_clean_corpus_reports_nothing(self, transcripts_dir):
        _write(transcripts_dir, "-Users-d-Projects-demo", "sess-a", [_entry("m1")])
        assert summarise_unpriced(transcripts_dir) == {}


class TestProjectWindowCosts:
    def test_window_excludes_older_spend(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("old", day="2026-06-01"), _entry("recent", day="2026-07-20")],
        )
        result = project_window_costs(
            window_days=14, projects_dir=transcripts_dir, today=date(2026, 7, 25)
        )
        assert result["total_usd"] == pytest.approx(25.0)
        assert result["projects"][0]["project"] == "demo"

    def test_projects_sorted_by_spend_descending(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-small",
            "s1",
            [_entry("m1", cwd="/workspace/Projects/small", usage=_usage(output_tokens=100_000))],
        )
        _write(
            transcripts_dir,
            "-Users-d-Projects-big",
            "s2",
            [_entry("m2", cwd="/workspace/Projects/big", usage=_usage(output_tokens=900_000))],
        )
        result = project_window_costs(
            window_days=14, projects_dir=transcripts_dir, today=date(2026, 7, 25)
        )
        names = [p["project"] for p in result["projects"]]
        assert names == ["big", "small"]

    def test_subagent_spend_is_broken_out_within_the_project_total(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-demo",
            "sess-a",
            [_entry("lead", sidechain=False), _entry("sub", sidechain=True)],
        )
        result = project_window_costs(
            window_days=14, projects_dir=transcripts_dir, today=date(2026, 7, 25)
        )
        demo = result["projects"][0]
        assert demo["total_usd"] == pytest.approx(50.0)
        assert demo["subagent_usd"] == pytest.approx(25.0)

    def test_cost_basis_is_declared_as_an_estimate(self, transcripts_dir):
        _write(transcripts_dir, "-Users-d-Projects-demo", "sess-a", [_entry("m1")])
        result = project_window_costs(projects_dir=transcripts_dir, today=date(2026, 7, 25))
        assert result["cost_basis"] == "token_estimate"


class TestProjectDailyCosts:
    def test_groups_by_day_then_project(self, transcripts_dir):
        _write(
            transcripts_dir,
            "-Users-d-Projects-alpha",
            "s1",
            [
                _entry("m1", day="2026-07-01", cwd="/workspace/Projects/alpha"),
                _entry("m2", day="2026-07-02", cwd="/workspace/Projects/alpha"),
            ],
        )
        _write(
            transcripts_dir,
            "-Users-d-Projects-beta",
            "s2",
            [_entry("m3", day="2026-07-01", cwd="/workspace/Projects/beta")],
        )
        result = project_daily_costs("2026-07-01", "2026-07-02", projects_dir=transcripts_dir)

        assert [d["day"] for d in result["days"]] == ["2026-07-01", "2026-07-02"]
        first = result["days"][0]
        assert first["total_usd"] == pytest.approx(50.0)
        assert {p["project"] for p in first["projects"]} == {"alpha", "beta"}

    def test_empty_range_returns_no_days(self, transcripts_dir):
        _write(transcripts_dir, "-Users-d-Projects-demo", "s1", [_entry("m1", day="2026-07-20")])
        result = project_daily_costs("2026-01-01", "2026-01-31", projects_dir=transcripts_dir)
        assert result["days"] == []
