"""Command line for demo, replay, and bounded public paper mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from paper_split_maker.config import ConfigError, load_config
from paper_split_maker.historical import (
    fetch_snapshot,
    load_historical_snapshot,
    parse_utc,
    run_historical,
    save_snapshot,
)
from paper_split_maker.session import run_demo, run_paper, run_replay

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_SCENARIO = PACKAGE_ROOT / "scenarios" / "symmetric_demo.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper-split-maker",
        description="Paper-only two-sided sell ledger for BTC Up/Down markets. Does not send orders.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the packaged deterministic synthetic demo")
    _add_out(demo)
    demo.add_argument("--config", type=Path, required=True)
    demo.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)

    replay = sub.add_parser(
        "replay", help="replay a labeled synthetic/fixture scenario"
    )
    _add_out(replay)
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--scenario", type=Path, required=True)

    paper = sub.add_parser("paper", help="bounded read-only public-data session")
    _add_out(paper)
    paper.add_argument("--config", type=Path, required=True)
    paper.add_argument("--duration-seconds", type=int, required=True)
    paper.add_argument("--max-markets", type=int, required=True)

    fetch = sub.add_parser(
        "historical-fetch",
        help="download a labeled public snapshot (no order book; prints and payouts)",
    )
    fetch.add_argument("--snapshot", type=Path, required=True)
    fetch.add_argument("--start", default="2026-11-06T17:00:00Z")
    fetch.add_argument("--end", default="2026-11-07T17:00:00Z")

    hist = sub.add_parser(
        "historical-replay",
        help="replay a reconstructed public snapshot on the paper ledger",
    )
    _add_out(hist)
    hist.add_argument("--config", type=Path, required=True)
    hist.add_argument("--snapshot", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "historical-fetch":
            snapshot = fetch_snapshot(parse_utc(args.start), parse_utc(args.end))
            save_snapshot(args.snapshot, snapshot)
            print(
                json.dumps(
                    {
                        "snapshot": str(args.snapshot),
                        "markets": len(snapshot["markets"]),
                        "label": snapshot["label"],
                        "synthetic_order_books": True,
                    },
                    indent=2,
                )
            )
            return 0
        config = load_config(args.config)
        out = Path(args.out)
        if args.command == "demo":
            summary = run_demo(config, out, args.scenario)
        elif args.command == "replay":
            summary = run_replay(config, args.scenario, out)
        elif args.command == "historical-replay":
            snapshot = load_historical_snapshot(args.snapshot)
            summary = run_historical(config, snapshot, out)
        else:
            summary = run_paper(
                config,
                out,
                duration_seconds=args.duration_seconds,
                max_markets=args.max_markets,
            )
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    except OSError as exc:
        print(f"io error: {exc}")
        return 2
    _print_summary(summary)
    return 0


def _add_out(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory for the ledger, logs, and summary",
    )


def _print_summary(summary: dict) -> None:
    brief = {
        "mode": summary.get("mode"),
        "resumed": summary.get("resumed"),
        "paper_only": summary.get("paper_only"),
        "live_orders": summary.get("live_orders"),
        "initial_capital": summary.get("initial_capital"),
        "loss_floor": summary.get("loss_floor"),
        "loss_budget_usdc": summary.get("loss_budget_usdc"),
        "cash": summary.get("cash"),
        "realized_pnl": summary.get("realized_pnl"),
        "worst_terminal_equity": summary.get("worst_terminal_equity"),
        "best_terminal_equity": summary.get("best_terminal_equity"),
        "rebate_estimate_unguaranteed": summary.get("rebate_estimate_unguaranteed"),
        "at_risk_cap": summary.get("at_risk_cap"),
        "public_data_ok": summary.get("public_data_ok"),
        "disclaimer_vi": summary.get("disclaimer_vi"),
        "net_pnl_usdc": (summary.get("historical") or {}).get("net_pnl_usdc"),
        "net_pnl_pct_of_initial": (summary.get("historical") or {}).get(
            "net_pnl_pct_of_initial"
        ),
        "worst_case_equity_minimum": (summary.get("historical") or {}).get(
            "worst_case_equity_minimum"
        ),
        "markets_declined": (summary.get("historical") or {}).get("markets_declined"),
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
