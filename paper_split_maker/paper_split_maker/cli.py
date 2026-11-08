"""Command line for demo, replay, and bounded public paper mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from paper_split_maker.config import ConfigError, load_config
from paper_split_maker.credentials import load_credentials
from paper_split_maker.live_gate import CONFIRMATION, live_gates
from paper_split_maker.historical import (
    fetch_snapshot,
    load_historical_snapshot,
    parse_utc,
    run_compare,
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

    compare = sub.add_parser(
        "historical-compare",
        help="replay every BTC 5m window in a day, old logic beside the new logic",
    )
    compare.add_argument("--old-config", type=Path, required=True)
    compare.add_argument("--new-config", type=Path, required=True)
    compare.add_argument("--out", type=Path, required=True)
    compare.add_argument("--start", default="2026-11-06T17:00:00Z")
    compare.add_argument("--end", default="2026-11-07T17:00:00Z")

    live = sub.add_parser(
        "live",
        help="opt-in live or dry-run. Paper is the default everywhere else.",
    )
    _add_out(live)
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--duration-seconds", type=int, required=True)
    live.add_argument("--max-markets", type=int, required=True)
    live.add_argument("--live", action="store_true")
    live.add_argument("--dry-run", action="store_true")
    live.add_argument("--yes-i-understand-live-risk", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "live":
            return _run_live(args)
        if args.command == "historical-compare":
            old = load_config(args.old_config)
            new = load_config(args.new_config)
            report = run_compare(
                old, new, parse_utc(args.start), parse_utc(args.end), Path(args.out)
            )
            print(
                json.dumps(
                    {
                        "sessions": report["sessions"],
                        "old_pnl": report["old"]["net_pnl_usdc"],
                        "new_pnl": report["new"]["net_pnl_usdc"],
                        "disclaimer": report["disclaimer"],
                    },
                    indent=2,
                )
            )
            return 0
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


def _run_live(args) -> int:
    config = load_config(args.config)
    allowed, reason = live_gates(
        config_live=config.live,
        cli_live=bool(args.live),
        cli_yes=bool(args.yes_i_understand_live_risk),
        is_tty=sys.stdin.isatty(),
    )
    if not allowed and sys.stdin.isatty() and args.live and config.live:
        print(f"Type exactly: {CONFIRMATION}")
        allowed, reason = live_gates(
            config_live=True,
            cli_live=True,
            cli_yes=False,
            typed=sys.stdin.readline(),
            is_tty=True,
        )
    if not allowed:
        print(f"live mode refused: {reason}. Staying paper-only.")
        return 2
    banner = {
        "mode": "dry-run" if args.dry_run else "live",
        "duration_seconds": args.duration_seconds,
        "loss_floor": str(config.loss_floor()),
        "session_max_loss": str(config.session_max_loss),
        "max_pair_loss": str(config.max_pair_loss),
        "per_market_budget": str(config.per_market_budget),
        "taker_repair": config.taker_repair,
        "slices_per_side": config.slices_per_side,
    }
    if args.dry_run:
        banner["signing"] = False
        banner["orders_sent"] = False
        print(json.dumps(banner, indent=2))
        summary = run_paper(
            config,
            Path(args.out),
            duration_seconds=args.duration_seconds,
            max_markets=args.max_markets,
        )
        summary["mode"] = "dry-run"
        summary["live_orders"] = False
        _print_plans(Path(args.out))
        _print_summary(summary)
        return 0
    creds = load_credentials(dict(os.environ))
    banner.update(creds.public_view())
    print(json.dumps(banner, indent=2))
    print(
        "LIVE session. Cancel-all runs on exit. "
        "Create the kill switch file to stop. This can lose money."
    )
    from paper_split_maker.live_runner import run_confirmed_live

    summary = run_confirmed_live(
        config,
        creds,
        Path(args.out),
        duration_seconds=args.duration_seconds,
        max_markets=args.max_markets,
    )
    _print_summary(summary)
    return 0


def _print_plans(out: Path) -> None:
    path = out / "logs" / "events.jsonl"
    if not path.exists():
        return
    print("dry-run plans (not sent):")
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") in {
            "quote_submit",
            "split",
            "merge",
            "cancel_request",
            "redeem",
        }:
            print(
                json.dumps(
                    {
                        "would": row["kind"],
                        "market_id": row.get("market_id"),
                        "outcome": row.get("outcome"),
                        "price": row.get("price"),
                        "size": row.get("size"),
                        "reason": row.get("reason"),
                    }
                )
            )


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
