"""Entry point for running cost-tracker as a module.

With no arguments this starts the MCP server on stdio, which is how every client
launches it. Subcommands are operator tools run by hand; they print to stdout and
must never be reachable from the bare invocation, because stdout on the server
path carries JSON-RPC and any stray byte corrupts the protocol.
"""

from __future__ import annotations

import argparse
import re
import sys

MONTH_SPEC_RE = re.compile(r"^(\d{4}-\d{2})=(\d+(?:\.\d+)?)$")


def _parse_month_spec(value: str) -> tuple[str, float]:
    match = MONTH_SPEC_RE.match(value.replace(",", "").replace("$", ""))
    if not match:
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM=AMOUNT (e.g. 2026-06=1234.56), got {value!r}"
        )
    return match.group(1), float(match.group(2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cost-tracker",
        description="Run the MCP server (no arguments) or an operator subcommand.",
    )
    sub = parser.add_subparsers(dest="command")

    calibrate = sub.add_parser(
        "calibrate",
        help="Fit the 1h cache-write multiplier against real billed totals.",
        description=(
            "Transcripts carry no cost field, so totals are estimated from tokens. The "
            "1h cache-write multiplier is the one genuinely uncertain term (Anthropic "
            "publishes 2x base input; ccusage appears to use the 1.25x 5m rate), and it "
            "moves a monthly total by over 12%. Supply one or more months of actual "
            "billed spend to settle it. Requires per-token API billing: a subscription "
            "plan price cannot calibrate a token estimator."
        ),
    )
    calibrate.add_argument(
        "--month",
        action="append",
        required=True,
        metavar="YYYY-MM=USD",
        type=_parse_month_spec,
        help="Actual billed total for a month, e.g. --month 2026-06=1234.56. Repeatable.",
    )
    calibrate.add_argument(
        "--candidate",
        action="append",
        type=float,
        metavar="MULTIPLIER",
        help="1h multiplier to evaluate. Repeatable. Defaults to 1.25 and 2.0.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    if args.command == "calibrate":
        # Imported lazily so the server path never pays for the scan machinery.
        from cost_tracker.calibrate import DEFAULT_CANDIDATES, calibrate, format_report

        observations = dict(args.month)
        candidates = tuple(args.candidate) if args.candidate else DEFAULT_CANDIDATES
        report = calibrate(observations, candidates=candidates)
        print(format_report(report))
        sys.exit(1 if "error" in report else 0)

    from cost_tracker.server import app

    app.run(transport="stdio")


if __name__ == "__main__":
    main()
