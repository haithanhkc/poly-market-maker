"""One command. Paper unless the live gates all pass."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from decimal import Decimal

from paper_split_maker.config import ConfigError, load_config, parse_series_list
from paper_split_maker.credentials import load_credentials
from paper_split_maker.envfile import prepare_run
from paper_split_maker.live_gate import CONFIRMATION, live_gates
from paper_split_maker.session import run_paper


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m paper_split_maker",
        description=(
            "Split USDT, post maker sells on Up and Down, reprice, "
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
        default=None,
        help="sessions to open (default 1, or BOT_MAX_MARKETS). Up to 500.",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="paper poll window; live uses max_runtime_seconds in the config",
    )
    parser.add_argument(
        "--strategy",
        choices=("single", "ladder", "dual_bid", "small_pair"),
        default=None,
        help="override the config strategy; dual_bid and small_pair are paper-only",
    )
    parser.add_argument(
        "--capital",
        default=None,
        help="bankroll for this run. Overrides the JSON and BOT_CAPITAL.",
    )
    parser.add_argument(
        "--max-loss-fraction",
        default=None,
        help="loss fraction for this run. Overrides the JSON and BOT_MAX_LOSS_FRACTION.",
    )
    parser.add_argument(
        "--series",
        default=None,
        help=(
            "comma-separated small_pair series, for example btc-updown-5m. "
            "Default is btc-updown-5m,btc-updown-15m."
        ),
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="read the API key, log in, and read USDT, then exit",
    )
    parser.add_argument("--yes-i-understand-live-risk", action="store_true")
    parser.add_argument(
        "--redeem-pending",
        type=Path,
        default=None,
        help="retry one deferred redeem file, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.check_connection:
            return _check_connection(args)
        if args.redeem_pending is not None:
            return _redeem_pending(args)
        if args.config is None or args.out is None:
            parser.error("--config and --out are required")
        if args.live:
            return _run_live(args)
        return _run_paper(args)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2


def _load(args):
    """JSON defaults, then .env, then BOT_* already set in the process.

    An explicit ``--max-markets`` wins over ``BOT_MAX_MARKETS``. The raw JSON
    bankroll is kept on the config so a buy strategy can put it back after
    the ladder derive step.
    """
    raw = load_config(args.config)
    explicit = getattr(args, "max_markets", None) is not None
    requested = args.max_markets if explicit else 1
    settings = prepare_run(
        args.config,
        os.environ,
        max_markets=requested,
        honor_env_markets=not explicit,
    )
    settings.config.json_initial_capital = raw.initial_capital
    settings.config.json_max_loss_fraction = raw.max_loss_fraction
    args.max_markets = settings.max_markets
    return settings.config


def _apply_strategy(config, args):
    choice = getattr(args, "strategy", None)
    if choice:
        config.strategy = choice
    chosen = getattr(args, "series", None)
    if chosen not in (None, ""):
        config.small_pair_series = parse_series_list(chosen)
    _honor_bankroll(config, args)
    return config


def _honor_bankroll(config, args):
    """Buy strategies keep the JSON bankroll. The ladder still uses the derive.

    ``prepare_run`` always replaces ``initial_capital`` with ``split_clip * 3``
    and ``max_loss_fraction`` with the ladder's worst-day ratio. A config of
    5000 and 0.20 therefore became capital 75 and a floor near 68.92 before
    the strategy flag was applied. ``--capital`` and ``--max-loss-fraction``
    still win, and so does ``BOT_CAPITAL`` / ``BOT_MAX_LOSS_FRACTION`` when
    the CLI flag is absent.
    """
    buy = config.strategy in {"dual_bid", "small_pair"}
    cli_capital = getattr(args, "capital", None)
    cli_fraction = getattr(args, "max_loss_fraction", None)
    if cli_capital not in (None, ""):
        try:
            config.initial_capital = Decimal(str(cli_capital))
        except (ArithmeticError, ValueError) as exc:
            raise ConfigError("capital is not a decimal") from exc
        config.capital_from_env = True
    elif buy and not getattr(config, "capital_from_env", False):
        saved = getattr(config, "json_initial_capital", None)
        if saved is not None:
            config.initial_capital = saved
    if cli_fraction not in (None, ""):
        try:
            config.max_loss_fraction = Decimal(str(cli_fraction))
        except (ArithmeticError, ValueError) as exc:
            raise ConfigError("max_loss_fraction is not a decimal") from exc
    elif buy and not getattr(config, "fraction_from_env", False):
        saved = getattr(config, "json_max_loss_fraction", None)
        if saved is not None:
            config.max_loss_fraction = saved
    if config.per_market_budget > config.initial_capital:
        config.per_market_budget = config.initial_capital
    if config.strategy == "small_pair":
        # Current window plus the next one, for each selected series.
        needed = 2 * len(config.small_pair_series)
        if config.max_open_markets < needed:
            config.max_open_markets = needed
    config.validate()


def _run_paper(args) -> int:
    config = _apply_strategy(_load(args), args)
    if config.live:
        print("config has live:true. Refusing to trade without --live.")
        return 2
    duration = args.duration_seconds or config.paper_duration_seconds
    summary = run_paper(
        config,
        Path(args.out),
        duration_seconds=duration,
        max_markets=args.max_markets,
        env=_credential_env(args.config),
    )
    _print_summary(summary)
    return 0


def _credential_env(config_path: Path | None) -> dict:
    """Process env first, then PREDICT_* lines from the .env beside the config.

    Values are not printed. A variable already set in the process wins.
    """
    env = dict(os.environ)
    if config_path is None:
        return env
    from paper_split_maker.envfile import read_env

    file_env = read_env(Path(config_path).resolve().parent / ".env")
    for key, value in file_env.items():
        if key.startswith("PREDICT_") and not str(env.get(key) or "").strip():
            env[key] = value
    return env


def _run_live(args) -> int:
    config = _apply_strategy(_load(args), args)
    if config.strategy in {"dual_bid", "small_pair"}:
        print(f"{config.strategy} is paper-only and does not send orders.")
        return 2
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
    creds = load_credentials(_credential_env(args.config))
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


def _check_connection(args) -> int:
    """Verify the key, the login, and the USDT balance. Nothing here is an order."""
    if args.config is None:
        print("config error: --config is required with --check-connection")
        return 2
    from paper_split_maker.live_clob import check_connection

    creds = load_credentials(_credential_env(args.config))
    series = "btc-updown-5m"
    try:
        from paper_split_maker.config import load_config

        series = load_config(args.config).series_slug
    except ConfigError:
        pass
    report = check_connection(creds, series=series)
    _safe_print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    ok = report.get("login_ok") and int(report.get("rounds_status") or 0) < 400 and report.get("usdt") != ""
    return 0 if ok else 2


def _redeem_pending(args) -> int:
    """Retry a deferred redeem file. Loads keys only on this path."""
    from paper_split_maker.live_runner import redeem_pending_once
    from paper_split_maker.normalize import resolution_settled

    path = args.redeem_pending
    creds = load_credentials(_credential_env(args.config))

    def resolved(condition_id: str) -> bool:
        from paper_split_maker.public_client import PublicReadClient

        payload = PublicReadClient(
            network=creds.network, api_key=creds.api_key
        ).fetch_resolution(condition_id)
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
        "polls": summary.get("polls"),
        "opened": summary.get("opened"),
        "discovery": summary.get("discovery"),
        "notes": summary.get("notes"),
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
