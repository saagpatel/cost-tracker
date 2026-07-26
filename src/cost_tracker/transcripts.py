"""Per-project, per-day cost computed directly from Claude Code transcripts.

``ccusage session`` rolls every session in a directory into one row carrying that
directory's all-time spend against a single date, and ``--since`` filters which
rows appear rather than the money inside them. Windowed per-project spend is
therefore not obtainable from that feed at any granularity.

The transcripts under ``~/.claude/projects/<dir>/<session>.jsonl`` are the source
ccusage itself reads, and they carry strictly more: a per-message ``timestamp``
(real day resolution), the exact ``model``, the full ``usage`` block including the
1h/5m cache-write split, ``isSidechain`` to separate subagent spend, and ``cwd`` —
the real absolute working directory, so attribution needs no lossy reverse-decode
of a dash-mangled directory name.

Cost is computed from tokens because transcripts carry no cost field, so every
figure here is an estimate (``cost_basis="token_estimate"``) and neither this nor
ccusage has been reconciled against a real invoice.

It deliberately diverges from ccusage in two ways, both of which raise this
module's totals:

1. **Subagent transcripts are included.** They live one level deeper, at
   ``<dir>/<parent-uuid>/subagents/agent-*.jsonl``, carry their own message ids,
   and are the majority of files in a real corpus. ``ccusage session`` reports
   them as a separate ``subagents`` bucket rather than against their project.
2. **1h cache writes bill at 2x base input**, versus 1.25x for 5m and 0.1x for
   reads. These are Anthropic's published multipliers, not an inference. ccusage
   prices all cache creation at the 5m rate — the documented failure mode for a
   parser that reads the flat ``cache_creation_input_tokens`` field and ignores
   the ``cache_creation.ephemeral_1h_input_tokens`` breakdown, which under-prices
   1h writes to 0.625x of the billed amount. Since 1h writes outnumber 5m by
   roughly 300:1 on a real corpus, that single term accounts for essentially the
   whole gap between this module and ccusage, and this module is the correct side
   of it.

A caution learned building this: an early version matched ccusage's grand total to
within 2.5% while being wrong twice over — it overcounted the main tier and
omitted the subagent tier entirely, and the two errors happened to cancel.
Validating an estimator on one aggregate number can hide compensating faults; check
the tiers separately.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
HOME_ADHOC_PROJECT = "home-adhoc"
TRANSIENT_PROJECT = "(transient)"

# Anthropic bills cache against the model's base input rate with fixed
# multipliers, so each model needs only its input/output pair maintained here.
#
# These three are Anthropic's published figures — do NOT "fix" the 1h multiplier
# down to 1.25 to match ccusage. ccusage prices all cache creation at the 5m rate,
# which under-prices 1h writes to 0.625x of what is actually billed; on a corpus
# where 1h writes outnumber 5m ~300:1 that understates a monthly total by ~14%.
# test_documented_cache_multipliers_are_not_silently_changed pins these.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1

# USD per 1M tokens, (input, output). Matched to the reviewed table in the
# operator's cost hook. NOTE: claude-sonnet-5 carries INTRODUCTORY pricing through
# 2026-08-31; list price after that is (3.0, 15.0) — REVISIT 2026-09-01.
BASE_RATES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

# Emitted by Claude Code for locally-generated messages that were never billed.
SYNTHETIC_MODEL = "<synthetic>"

# Agent worktrees are per-project isolation checkouts, not projects. Left
# unresolved they collapse every project's parallel-agent spend into one fake
# bucket named after the container.
#
# Two layouts exist. Worktrees nested inside a project
# (``Projects/<project>/.claude/worktrees/<branch>``) already resolve correctly,
# because the project name is still the first segment under the container. Only
# the sibling layout (``Projects/_claude-worktrees/<project>-<branch>``) needs
# unwrapping, and it appears under at least four container names
# (``_claude-``/``_fable-``/``_codex-worktrees`` and ``.worktrees``), so match the
# shape instead of enumerating. Requiring the leading ``_`` or ``.`` keeps a real
# project such as ``worktree-manager`` from being mistaken for a container.

# Directories that hold projects one level down rather than being one themselves.
PROJECT_CONTAINERS = ("Projects", "Documents")


@dataclass(frozen=True)
class UsageRecord:
    """One billed assistant message."""

    day: str
    project: str
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    is_subagent: bool
    session_id: str


def resolve_rate(model: str | None) -> tuple[float, float] | None:
    """Return (input, output) USD/MTok for a model id, or None if unpriced.

    Matching is by prefix so dated ids (``claude-haiku-4-5-20251001``) resolve to
    their family. Returning None rather than a zero is deliberate: an unpriced
    model must surface as an explicit gap, never be silently costed at nothing.
    """
    if not model:
        return None
    for prefix, rate in BASE_RATES.items():
        if model.startswith(prefix):
            return rate
    return None


def compute_cost(model: str | None, usage: dict[str, Any]) -> float | None:
    """Cost one message's usage block, or None when the model is unpriced.

    The 1h/5m cache split matters far more than it looks: 1h writes bill at 2x
    base input versus 1.25x for 5m, and on a real corpus 1h writes outnumber 5m
    by roughly 300:1. Costing everything at the 5m rate understates the total by
    an order of magnitude more than every other correction combined.
    """
    rate = resolve_rate(model)
    if rate is None:
        return None
    input_rate, output_rate = rate

    cache_creation = usage.get("cache_creation") or {}
    write_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
    write_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
    if not (write_1h or write_5m):
        # Older transcripts carry only the undifferentiated total. Those predate
        # 1h caching, so the 5m rate is the correct reading, not a fallback guess.
        write_5m = usage.get("cache_creation_input_tokens", 0)

    millions = (
        usage.get("input_tokens", 0) * input_rate
        + usage.get("output_tokens", 0) * output_rate
        + usage.get("cache_read_input_tokens", 0) * input_rate * CACHE_READ_MULTIPLIER
        + write_5m * input_rate * CACHE_WRITE_5M_MULTIPLIER
        + write_1h * input_rate * CACHE_WRITE_1H_MULTIPLIER
    )
    return millions / 1_000_000


def is_worktree_container(name: str) -> bool:
    """True for a directory that holds worktrees rather than being a project."""
    return name.startswith(("_", ".")) and "worktree" in name.lower()


@lru_cache(maxsize=8)
def _known_project_names(home_str: str) -> tuple[str, ...]:
    """Project names on disk, longest first so the greediest prefix wins."""
    home_path = Path(home_str)
    names: set[str] = set()
    for root in (home_path / "Projects", home_path / ".local" / "share"):
        try:
            names.update(child.name for child in root.iterdir() if child.is_dir())
        except OSError:
            continue
    return tuple(sorted((n for n in names if not is_worktree_container(n)), key=len, reverse=True))


def _resolve_worktree(dirname: str, home: Path) -> str:
    """Map a worktree directory back to the project it is a checkout of.

    Worktrees are named ``<project>-<branch-slug>``, which is ambiguous on its
    own because both halves may contain dashes. Matching against the projects
    that actually exist on disk — longest name first — resolves it exactly, the
    same trick session_sync uses for dash-mangled ccusage directory names.

    An unmatched worktree keeps its own directory name rather than being folded
    into a guess, so a genuinely new project stays visible instead of silently
    inheriting another project's spend.
    """
    for name in _known_project_names(str(home)):
        if dirname == name or dirname.startswith(f"{name}-"):
            return name
    return dirname


def project_from_cwd(cwd: str | None, home: Path | None = None) -> str | None:
    """Map a transcript's real working directory to a project name.

    Unlike the ccusage path, this needs no reverse-decode: ``cwd`` is the actual
    absolute directory, so a project whose name contains dashes or spaces
    resolves exactly rather than ambiguously.
    """
    if not cwd:
        return None
    home_path = home or Path.home()
    try:
        path = Path(cwd)
    except (TypeError, ValueError):
        return None

    try:
        relative = path.relative_to(home_path)
    except ValueError:
        # Outside home. Sandboxes and temp dirs are real spend but not a project,
        # so bucket them rather than letting them vanish or masquerade as one.
        # This test must come *after* the home check: on macOS the home directory
        # itself can resolve beneath /private/var, which would otherwise make
        # every path look transient.
        if any(part in {"private", "tmp", "var"} for part in path.parts[:3]):
            return TRANSIENT_PROJECT
        return path.name or None

    segments = [p for p in relative.parts if p]
    if not segments:
        return HOME_ADHOC_PROJECT

    # ~/Projects/<name>/... and ~/.local/share/<name> both name the project one
    # level below a container directory rather than at the top level.
    if segments[0] in PROJECT_CONTAINERS and len(segments) >= 2:
        if is_worktree_container(segments[1]):
            return _resolve_worktree(segments[2], home_path) if len(segments) >= 3 else None
        return segments[1]
    if segments[:2] == [".local", "share"] and len(segments) >= 3:
        return segments[2]
    if segments[0] == ".claude":
        return segments[1] if len(segments) >= 2 else HOME_ADHOC_PROJECT
    return segments[0]


def _iter_billed_messages(
    projects_dir: Path,
) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Yield (entry, message, usage) for each deduplicated billed message.

    Traversal is **recursive**. Subagent transcripts live one level deeper, at
    ``<project-dir>/<parent-session-uuid>/subagents/agent-*.jsonl``, and they are
    the majority of files in a real corpus. A one-level glob silently drops every
    one of them along with all their spend.

    Deduplication is mandatory, not defensive: resumed and forked sessions
    re-serialize earlier messages into new transcript files, and roughly 60% of
    usage lines are repeats. Summing without deduplicating overstates spend about
    2.5x. Subagent messages carry their own ids and are never duplicates of the
    parent's, so recursion adds real spend rather than double-counting.

    This walk is shared by every scanner in the module so their coverage cannot
    drift apart.
    """
    seen: set[str] = set()
    for path in sorted(projects_dir.rglob("*.jsonl")):
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                # Cheap prefilter: most transcript lines are user turns and tool
                # results, and parsing them costs far more than this substring.
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                message_id = message.get("id")
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                yield entry, message, usage


def iter_usage_records(
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    *,
    since: str | None = None,
    until: str | None = None,
) -> Iterator[UsageRecord]:
    """Yield one deduplicated UsageRecord per billed assistant message."""
    for entry, message, usage in _iter_billed_messages(projects_dir):
        day = (entry.get("timestamp") or "")[:10]
        if not day:
            continue
        if since and day < since:
            continue
        if until and day > until:
            continue

        model = message.get("model")
        cost = compute_cost(model, usage)
        if cost is None:
            # Unpriced models are reported by summarise_unpriced(), never folded
            # into a total as zero.
            continue

        project = project_from_cwd(entry.get("cwd"))
        if project is None:
            continue

        cache_creation = usage.get("cache_creation") or {}
        write = cache_creation.get("ephemeral_1h_input_tokens", 0) + cache_creation.get(
            "ephemeral_5m_input_tokens", 0
        )
        if not write:
            write = usage.get("cache_creation_input_tokens", 0)

        yield UsageRecord(
            day=day,
            project=project,
            model=model or "unknown",
            cost_usd=cost,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=write,
            is_subagent=bool(entry.get("isSidechain")),
            session_id=str(entry.get("sessionId") or ""),
        )


def summarise_unpriced(projects_dir: Path = CLAUDE_PROJECTS_DIR) -> dict[str, int]:
    """Count billed messages whose model has no rate, keyed by model id.

    A model missing from BASE_RATES would otherwise contribute nothing to every
    total while every reported number still looked healthy. Callers surface this
    so a model launch shows up as a named gap the same day it starts being used.
    """
    counts: dict[str, int] = defaultdict(int)
    for _entry, message, _usage in _iter_billed_messages(projects_dir):
        model = message.get("model")
        if model != SYNTHETIC_MODEL and resolve_rate(model) is None:
            counts[model or "(missing)"] += 1
    return dict(counts)


def project_window_costs(
    window_days: int = 14,
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Per-project spend over a genuine rolling window, highest first.

    This is the query ccusage cannot answer. Every message is attributed to its
    own day and its own working directory, so the window is real rather than a
    lifetime total filtered by a single stale timestamp.
    """
    anchor = today or date.today()
    since = (anchor - timedelta(days=window_days)).isoformat()
    until = anchor.isoformat()

    totals: dict[str, float] = defaultdict(float)
    subagent: dict[str, float] = defaultdict(float)
    sessions: dict[str, set[str]] = defaultdict(set)
    grand_total = 0.0

    for record in iter_usage_records(projects_dir, since=since, until=until):
        totals[record.project] += record.cost_usd
        grand_total += record.cost_usd
        if record.is_subagent:
            subagent[record.project] += record.cost_usd
        if record.session_id:
            sessions[record.project].add(record.session_id)

    projects = [
        {
            "project": name,
            "total_usd": round(total, 6),
            "subagent_usd": round(subagent.get(name, 0.0), 6),
            "session_count": len(sessions.get(name, ())),
        }
        for name, total in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "since": since,
        "until": until,
        "window_days": window_days,
        "total_usd": round(grand_total, 6),
        "projects": projects,
        "cost_basis": "token_estimate",
    }


def project_daily_costs(
    since: str,
    until: str | None = None,
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> dict[str, Any]:
    """Spend broken down by (day, project) across an explicit date range.

    Args:
        since: Inclusive start date, ``YYYY-MM-DD``.
        until: Inclusive end date, ``YYYY-MM-DD``. Defaults to today.
    """
    end = until or date.today().isoformat()
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for record in iter_usage_records(projects_dir, since=since, until=end):
        totals[(record.day, record.project)] += record.cost_usd

    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (day, project), cost in totals.items():
        days[day].append({"project": project, "total_usd": round(cost, 6)})

    return {
        "since": since,
        "until": end,
        "cost_basis": "token_estimate",
        "days": [
            {
                "day": day,
                "total_usd": round(sum(p["total_usd"] for p in entries), 6),
                "projects": sorted(entries, key=lambda p: p["total_usd"], reverse=True),
            }
            for day, entries in sorted(days.items())
        ],
    }
