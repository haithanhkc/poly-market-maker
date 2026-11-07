"""Command line for demo, replay, and bounded public paper mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from paper_split_maker.config import ConfigError, load_config
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

    replay = sub.add_parser("replay", help="replay a labeled synthetic/fixture scenario")
    _add_out(replay)
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--scenario", type=Path, required=True)

    paper = sub.add_parser("paper", help="bounded read-only public-data session")
    _add_out(paper)
    paper.add_argument("--config", type=Path, required=True)
    paper.add_argument("--duration-seconds", type=int, required=True)
    paper.add_argument("--max-markets", type=int, required=True)

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        out = Path(args.out)
        if args.command == "demo":
            summary = run_demo(config, out, args.scenario)
        elif args.command == "replay":
            summary = run_replay(config, args.scenario, out)
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
    parser.add_argument("--out", type=Path, required=True, help="directory for the ledger, logs, and summary")


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
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
