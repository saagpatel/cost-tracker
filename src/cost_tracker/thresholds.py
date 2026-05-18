"""Threshold check logic for daily spend alerts."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path.home() / ".config" / "cost-tracker" / "thresholds.toml"
_DEFAULT_THRESHOLDS = [5.0, 15.0, 30.0]


def load_thresholds(config_path: Path = _CONFIG_PATH) -> list[float]:
    """Load thresholds from TOML config, falling back to defaults."""
    if not config_path.exists():
        return list(_DEFAULT_THRESHOLDS)
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        raw = data.get("thresholds_usd", _DEFAULT_THRESHOLDS)
        thresholds = sorted(float(v) for v in raw)
        return thresholds if thresholds else list(_DEFAULT_THRESHOLDS)
    except Exception:  # noqa: BLE001 — bad config falls back to defaults
        return list(_DEFAULT_THRESHOLDS)


def check_thresholds(today_usd: float, thresholds: list[float] | None = None) -> dict[str, Any]:
    """
    Compare today_usd against threshold values.

    Returns:
        {
            today_usd: float,
            thresholds_crossed: list[float],   # thresholds already breached
            next_threshold: float | None,       # next threshold not yet crossed
            headroom_usd: float | None,         # dollars until next_threshold
        }
    """
    if thresholds is None:
        thresholds = load_thresholds()

    sorted_t = sorted(thresholds)
    crossed = [t for t in sorted_t if today_usd >= t]
    remaining = [t for t in sorted_t if today_usd < t]

    next_threshold = remaining[0] if remaining else None
    headroom = round(next_threshold - today_usd, 2) if next_threshold is not None else None

    return {
        "today_usd": round(today_usd, 6),
        "thresholds_crossed": crossed,
        "next_threshold": next_threshold,
        "headroom_usd": headroom,
    }
