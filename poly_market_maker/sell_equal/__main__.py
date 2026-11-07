"""Paper demo and replay entry point.

Windows:
    py -3 -m poly_market_maker.sell_equal demo
    py -3 -m unittest tests.test_sell_equal
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from poly_market_maker.sell_equal.replay import load_script, run_replay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poly_market_maker.sell_equal")
    parser.add_argument("command", choices=("demo", "replay", "live"))
    parser.add_argument("--script", default="config/replay_equal_usd.json")
    parser.add_argument("--config", default="config/sell_equal_value.example.json")
    parser.add_argument(
        "--send",
        action="store_true",
        help="With live opt-in, run one prepared cycle. Omit to send nothing.",
    )
    args = parser.parse_args(argv)
    if args.command == "live":
        return _live(args)
    script = "config/replay_equal_usd.json" if args.command == "demo" else args.script
    config, steps = load_script(script)
    engine, _broker, _checks = run_replay(config, steps)
    print("mode=legacy_equal_gross_usd")
    _print_summary(engine)
    return 0


def _live(args) -> int:
    from poly_market_maker.sell_equal.live import LiveDisabled, require_live_opt_in

    try:
        require_live_opt_in()
    except LiveDisabled as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.send:
        print(
            "Live opt-in is set, but --send was omitted. No order was sent.",
            file=sys.stderr,
        )
        return 2
    from datetime import datetime, timezone

    from poly_market_maker.sell_equal.config import load_config
    from poly_market_maker.sell_equal.live import (
        build_client_from_env,
        fetch_public_json,
        run_prepared_cycle,
    )

    config = load_config(args.config)
    client = build_client_from_env()
    try:
        engine = run_prepared_cycle(
            config, client, fetch_public_json, datetime.now(timezone.utc)
        )
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()
    _print_summary(engine)
    return 0


def _print_summary(engine) -> None:
    sold = engine.state.gross_sold
    print("value_basis=gross")
    print("gross_up=" + _money(sold["up"]))
    print("gross_down=" + _money(sold["down"]))
    hedges = [order for order in engine.open_orders() if order.role == "hedge"]
    if not hedges:
        print("open_hedge=none")
        return
    hedge = hedges[0]
    print(
        "open_hedge outcome=%s price=%s size=%s gross=%s"
        % (
            hedge.outcome,
            hedge.price,
            hedge.original_size,
            _money(hedge.original_size * hedge.price),
        )
    )


def _money(value: Decimal) -> str:
    return format(value, "f")


if __name__ == "__main__":
    raise SystemExit(main())
