"""One command. Paper unless the live gates all pass."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from paper_split_maker.config import ConfigError, load_config
from paper_split_maker.credentials import load_credentials
from paper_split_maker.live_gate import CONFIRMATION, live_gates
from paper_split_maker.session import run_paper


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m paper_split_maker",
        description=(
            "Split pUSD, post maker sells on Up and Down, reprice, "
            "unwind a filled leg, then merge or redeem. "
            "Paper unless --live, --yes-i-understand-live-risk, and "
            "live:true are all set."
        ),
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        help="directory for the ledger, logs, and summary",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=1,
        help="how many sessions to split, quote, and finish (default 1)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="paper poll window; live uses max_runtime_seconds in the config",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--yes-i-understand-live-risk", action="store_true")
    parser.add_argument(
        "--redeem-pending",
        type=Path,
        default=None,
        help="retry one deferred redeem file, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.redeem_pending is not None:
            return _redeem_pending(args.redeem_pending)
        if args.config is None or args.out is None:
            parser.error("--config and --out are required")
        if args.live:
            return _run_live(args)
        return _run_paper(args)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2


def _run_paper(args) -> int:
    config = load_config(args.config)
    if config.live:
        print("config has live:true. Refusing to trade without --live.")
        return 2
    duration = args.duration_seconds or config.paper_duration_seconds
    summary = run_paper(
        config,
        Path(args.out),
        duration_seconds=duration,
        max_markets=args.max_markets,
    )
    _print_summary(summary)
    return 0


def _run_live(args) -> int:
    config = load_config(args.config)
    allowed, reason = live_gates(
        config_live=config.live,
        cli_live=True,
        cli_yes=bool(args.yes_i_understand_live_risk),
        is_tty=sys.stdin.isatty(),
    )
    if not allowed and sys.stdin.isatty() and config.live:
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
    duration = args.duration_seconds or int(config.max_runtime_seconds)
    creds = load_credentials(dict(os.environ))
    banner = {
        "mode": "live",
        "loss_floor": str(config.loss_floor()),
        "loss_budget_usdc": str(config.loss_budget()),
        "max_markets": args.max_markets,
        "max_runtime_seconds": duration,
        "kill_switch_file": config.kill_switch_file,
    }
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
        duration_seconds=duration,
        max_markets=args.max_markets,
        read_chain_balance=True,
    )
    _print_summary(summary)
    return 0


def _redeem_pending(path: Path) -> int:
    """Retry a deferred redeem file. Loads keys only on this path."""
    from paper_split_maker.live_runner import redeem_pending_once
    from paper_split_maker.normalize import resolution_settled

    creds = load_credentials(dict(os.environ))

    def resolved(condition_id: str) -> bool:
        from paper_split_maker.public_client import PublicReadClient

        payload = PublicReadClient().fetch_resolution(condition_id)
        return resolution_settled(condition_id, payload)

    def submit(record: dict) -> None:
        from paper_split_maker.chain_send import submit_position

        submit_position(
            creds,
            "redeem",
            str(record.get("condition_id") or ""),
            str(record.get("amount") or "0"),
            "v1",
            False,
            500_000,
            "0",
            "0",
        )

    result = redeem_pending_once(path, submit=submit, resolved=resolved)
    print(json.dumps(result))
    return 0


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
    text = json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=True)
    _safe_print(text)


def _safe_print(text: str) -> None:
    """Print without crashing on consoles such as Windows gbk/cp1252."""
    try:
        print(text)
    except UnicodeEncodeError:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(text.encode("utf-8", "replace") + b"\n")
            stream.flush()
        else:
            print(text.encode("ascii", "backslashreplace").decode("ascii"))


if __name__ == "__main__":
    raise SystemExit(main())
