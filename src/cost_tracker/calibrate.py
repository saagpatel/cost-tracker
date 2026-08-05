"""Calibrate the token-cost estimator against a real Anthropic invoice.

Transcripts carry no cost field, so every figure this package reports is computed
from tokens times a rate table. The rates themselves are settled — Anthropic
publishes 1.25x base input for a 5-minute cache write, 2x for 1-hour, and 0.1x for
a read — and ``transcripts.py`` implements exactly those. This module is not
needed to choose them.

What it is for is verifying the whole estimator against ground truth: confirming
the rate table still matches reality after a price change, catching a model whose
rates were never added, and quantifying how much billed spend has no transcript
behind it. Given one or more (month, actually billed) pairs, it scans each month
once, reduces it to per-model token totals, then evaluates candidate multipliers
against those totals analytically. One scan, any number of hypotheses.

A sweep over the 1h multiplier is the default because that term is where a
regression is most likely to be introduced: ccusage prices all cache creation at
the 5m rate, so anyone reconciling against ccusage will be tempted to "fix" 2x down
to 1.25x. That would match ccusage and understate real spend by roughly 14%.

Two conditions make an invoice unusable for this, and both are detected and
reported rather than silently producing a confident wrong answer:

- **Subscription billing.** On a Max or Team plan, Claude Code usage is not billed
  per token at all; the console shows the plan price. Reconciling against that
  number calibrates nothing. Only API/console token billing works here.
- **Usage outside Claude Code.** Transcripts cover Claude Code only. Direct API
  calls, other tools, and other machines all land on the same invoice but leave no
  transcript, so the estimate is a floor. When every candidate underestimates, the
  residual is unexplained usage, not a bad multiplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cost_tracker.transcripts import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    CLAUDE_PROJECTS_DIR,
    SYNTHETIC_MODEL,
    _iter_billed_messages,
    resolve_rate,
)

# The competing readings of the 1h cache-write rate. 2.0 is Anthropic's published
# figure; 1.25 is the 5m rate, which is what ccusage appears to apply to all cache
# creation. Anything else can be passed explicitly.
DEFAULT_CANDIDATES: tuple[float, ...] = (1.25, 2.0)

# Below this, a month's residual is within the noise of rounding and mid-month
# price changes; above it, something structural is unaccounted for.
MATCH_TOLERANCE_PCT = 2.0


@dataclass
class TokenTotals:
    """Summed token counts for one model over one month."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    def add(self, usage: dict[str, Any]) -> None:
        cache_creation = usage.get("cache_creation") or {}
        write_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
        write_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
        if not (write_1h or write_5m):
            write_5m = usage.get("cache_creation_input_tokens", 0)

        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        self.cache_write_5m_tokens += write_5m
        self.cache_write_1h_tokens += write_1h


def month_token_totals(
    months: set[str], projects_dir: Path = CLAUDE_PROJECTS_DIR
) -> dict[str, dict[str, TokenTotals]]:
    """Scan once, returning {month: {model: TokenTotals}} for the months requested.

    Reducing to token totals up front is what makes the sweep cheap: candidate
    multipliers are then pure arithmetic over these sums rather than another pass
    over the corpus.
    """
    result: dict[str, dict[str, TokenTotals]] = {month: {} for month in months}
    for entry, message, usage in _iter_billed_messages(projects_dir):
        day = (entry.get("timestamp") or "")[:10]
        month = day[:7]
        if month not in result:
            continue
        model = message.get("model")
        if model == SYNTHETIC_MODEL or resolve_rate(model) is None:
            continue
        result[month].setdefault(model or "unknown", TokenTotals()).add(usage)
    return result


def total_under(
    totals_by_model: dict[str, TokenTotals],
    *,
    cache_write_1h_multiplier: float,
    month: str | None = None,
) -> float:
    """Cost a month's token totals under one candidate 1h multiplier.

    ``month`` ("YYYY-MM") prices the totals as of that month, which matters when
    a model family crosses an intro-pricing cutover; None means current rates.
    """
    total = 0.0
    for model, totals in totals_by_model.items():
        rate = resolve_rate(model, f"{month}-01" if month else None)
        if rate is None:
            continue
        input_rate, output_rate = rate
        total += (
            totals.input_tokens * input_rate
            + totals.output_tokens * output_rate
            + totals.cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
            + totals.cache_write_5m_tokens * input_rate * CACHE_WRITE_5M_MULTIPLIER
            + totals.cache_write_1h_tokens * input_rate * cache_write_1h_multiplier
        ) / 1_000_000
    return total


def calibrate(
    observations: dict[str, float],
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATES,
) -> dict[str, Any]:
    """Rank candidate 1h cache multipliers against real billed totals.

    Args:
        observations: {"YYYY-MM": actually_billed_usd}. One month is enough to
            pick a winner; more months make the result harder to fit by accident.
        candidates: 1h cache-write multipliers to evaluate.

    Returns a ranked report. ``best`` is the closest candidate, but read
    ``warnings`` before adopting it: a month with no transcript data, or a
    universal underestimate, means the comparison is not measuring the multiplier.
    """
    if not observations:
        return {
            "error": "no_observations",
            "detail": "pass at least one month and its billed total",
        }

    totals = month_token_totals(set(observations), projects_dir)
    warnings: list[str] = []

    empty = [month for month, models in totals.items() if not models]
    if empty:
        warnings.append(
            f"no transcript data for {', '.join(sorted(empty))} — those months cannot "
            "constrain the multiplier and are excluded from the fit"
        )

    usable = {m: v for m, v in observations.items() if totals.get(m)}
    if not usable:
        return {
            "error": "no_usable_months",
            "detail": "every requested month has no transcript data",
            "warnings": warnings,
        }

    hypotheses: list[dict[str, Any]] = []
    for multiplier in candidates:
        per_month = []
        est_sum = act_sum = 0.0
        for month in sorted(usable):
            estimated = total_under(
                totals[month], cache_write_1h_multiplier=multiplier, month=month
            )
            actual = usable[month]
            est_sum += estimated
            act_sum += actual
            per_month.append(
                {
                    "month": month,
                    "estimated_usd": round(estimated, 2),
                    "actual_usd": round(actual, 2),
                    "residual_usd": round(estimated - actual, 2),
                    "residual_pct": round((estimated / actual - 1) * 100, 2) if actual else None,
                }
            )
        hypotheses.append(
            {
                "cache_write_1h_multiplier": multiplier,
                "estimated_usd": round(est_sum, 2),
                "actual_usd": round(act_sum, 2),
                "abs_error_usd": round(abs(est_sum - act_sum), 2),
                "error_pct": round((est_sum / act_sum - 1) * 100, 2) if act_sum else None,
                "per_month": per_month,
            }
        )

    hypotheses.sort(key=lambda h: h["abs_error_usd"])
    best = hypotheses[0]

    if all((h["error_pct"] or 0) < 0 for h in hypotheses):
        warnings.append(
            "every candidate underestimates the invoice. Transcripts cover Claude Code "
            "only, so the estimate is a floor — the residual is most likely usage with "
            "no transcript (direct API calls, another machine, or another tool) rather "
            "than a wrong multiplier. Calibration is not conclusive here."
        )
    if all((h["error_pct"] or 0) > 0 for h in hypotheses):
        warnings.append(
            "every candidate overestimates the invoice. Check whether the billed total "
            "reflects subscription pricing rather than per-token API billing — a plan "
            "price cannot calibrate a token estimator."
        )
    if best["error_pct"] is not None and abs(best["error_pct"]) > MATCH_TOLERANCE_PCT:
        warnings.append(
            f"best candidate is still {best['error_pct']:+.2f}% off, outside the "
            f"{MATCH_TOLERANCE_PCT}% tolerance — treat the ranking as directional and "
            "look for an unmodelled term before pinning it."
        )

    return {
        "months": sorted(usable),
        "hypotheses": hypotheses,
        "best": {
            "cache_write_1h_multiplier": best["cache_write_1h_multiplier"],
            "error_pct": best["error_pct"],
            "within_tolerance": (
                best["error_pct"] is not None and abs(best["error_pct"]) <= MATCH_TOLERANCE_PCT
            ),
        },
        "warnings": warnings,
    }


def format_report(report: dict[str, Any]) -> str:
    """Render a calibration report for a terminal."""
    if "error" in report:
        return f"calibration failed: {report['error']} — {report.get('detail', '')}"

    lines = [
        f"Calibrating against {len(report['months'])} month(s): {', '.join(report['months'])}",
        "",
    ]
    lines.append(f"{'1h multiplier':>14}{'estimated':>14}{'actual':>13}{'error':>12}")
    for h in report["hypotheses"]:
        pct = f"{h['error_pct']:+.2f}%" if h["error_pct"] is not None else "n/a"
        lines.append(
            f"{h['cache_write_1h_multiplier']:>14.2f}"
            f"{h['estimated_usd']:>14,.2f}{h['actual_usd']:>13,.2f}{pct:>12}"
        )

    best = report["best"]
    verdict = "within tolerance" if best["within_tolerance"] else "NOT within tolerance"
    lines += ["", f"best fit: 1h multiplier {best['cache_write_1h_multiplier']} ({verdict})"]

    if len(report["months"]) > 1:
        lines.append("")
        lines.append("per-month residuals for the best fit:")
        for row in report["hypotheses"][0]["per_month"]:
            pct = f"{row['residual_pct']:+.2f}%" if row["residual_pct"] is not None else "n/a"
            lines.append(
                f"  {row['month']}  est {row['estimated_usd']:>10,.2f}  "
                f"actual {row['actual_usd']:>10,.2f}  {pct:>9}"
            )

    for warning in report["warnings"]:
        lines += ["", f"WARNING: {warning}"]
    return "\n".join(lines)
