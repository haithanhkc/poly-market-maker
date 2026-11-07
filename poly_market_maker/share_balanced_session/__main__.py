"""Paper demo for the share-balanced session. There is no live sender.

Windows:
    py -3 -m poly_market_maker.share_balanced_session demo
    py -3 -m unittest tests.test_share_balanced_session
"""

from __future__ import annotations

import argparse
import sys

from poly_market_maker.share_balanced_session.replay import load_script, run_replay
from poly_market_maker.share_balanced_session.status import format_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poly_market_maker.share_balanced_session")
    parser.add_argument("command", choices=("demo", "replay", "live"))
    parser.add_argument("--script", default="config/replay_share_balanced_session.json")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Ignored. This mode has no live sender.",
    )
    args = parser.parse_args(argv)
    if args.command == "live":
        print(
            "share_balanced_session has no live sender. "
            "Maker-only paper/replay cannot be switched to taker or production orders.",
            file=sys.stderr,
        )
        return 2
    script = (
        "config/replay_share_balanced_session.json"
        if args.command == "demo"
        else args.script
    )
    config, steps = load_script(script)
    session, _broker = run_replay(config, steps)
    print(format_status(session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
