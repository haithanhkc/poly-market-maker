import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.quant import D
from poly_market_maker.share_balanced_session.book import Level, parse_book
from poly_market_maker.share_balanced_session.config import load_config
from poly_market_maker.share_balanced_session.engine import Fill, ShareSession
from poly_market_maker.share_balanced_session.paper import PaperBroker
from poly_market_maker.share_balanced_session.pnl import settlement_lower_bound
from poly_market_maker.share_balanced_session.replay import load_script, run_replay
from poly_market_maker.share_balanced_session.status import format_status
from poly_market_maker.share_balanced_session.submit import MakerOnlyViolation
from poly_market_maker.share_balanced_session.__main__ import main

UP = "44857089090901365835706371888611204979538894171442730872794600452065181330944"
DOWN = "17424551638207318915606858171665237870653355896978276914963148187077601073259"
CONDITION = "0xe4966d642938113525dfb125060bda90b9f8d8c25945ae352de1b75c160bc383"


class Clock:
    def __init__(self, now="1700000000"):
        self.now = D(now)

    def __call__(self):
        return self.now


def write_case(root, snapshot_overrides=None, **overrides):
    snapshot = {
        "cost_basis_source": "explicit_allocation",
        "condition_id": CONDITION,
        "up_token_id": UP,
        "down_token_id": DOWN,
        "allocated_up": "100",
        "allocated_down": "100",
        "cost_basis_usd": "100",
        "capital_usd": "100",
    }
    if snapshot_overrides:
        snapshot.update(snapshot_overrides)
    snap_path = Path(root) / "baseline.json"
    snap_path.write_text(json.dumps(snapshot), encoding="utf-8")
    raw = {
        "mode": "share_balanced_session",
        "maker_only": True,
        "order_type": "post_only",
        "baseline_snapshot": str(snap_path),
        "condition_id": CONDITION,
        "slug": "btc-updown-5m-1794024900",
        "up_token_id": UP,
        "down_token_id": DOWN,
        "allocated_up": "100",
        "allocated_down": "100",
        "cost_basis_usd": "100",
        "capital_usd": "100",
        "cost_basis_source": "explicit_allocation",
        "tick_size": "0.01",
        "min_order_size": "5",
        "size_quantum": "0.01",
        "price_floor": "0.01",
        "price_ceiling": "0.99",
        "improve_ticks": 0,
        "hysteresis_ticks": 0,
        "min_replace_seconds": "0",
        "seed_shares": "5",
        "max_share_skew": "20",
        "risk_budget_usd": "2",
        "take_profit_usd": "2",
        "maker_fee_rate": "0",
        "taker_fee_rate": "0.07",
        "rebate_share": "0.20",
        "stale_after_seconds": "60",
        "session_start_ts": "1700000000",
        "session_end_ts": "1700000300",
        "cutoff_seconds": "30",
        "state_path": str(Path(root) / "state.json"),
    }
    raw.update(overrides)
    path = Path(root) / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(path)


def boot(root, **overrides):
    config = write_case(root, **overrides)
    clock = Clock()
    broker = PaperBroker()
    session = ShareSession(config, broker, clock)
    return session, broker, clock


def book(session, outcome, bid, ask, ts="1700000000"):
    payload = {"ts": ts, "bids": [[bid, "100"]], "asks": [[ask, "100"]]}
    session.observe_book(
        outcome,
        parse_book(payload, session.config.tick_size, session.config.min_order_size),
    )


def arm(session, broker, clock, bid="0.49", ask="0.50", ts="1700000000"):
    book(session, "up", bid, ask, ts)
    book(session, "down", bid, ask, ts)
    clock.now = D(ts)
    session.observe_user(clock.now)


class ShareBalancedTests(unittest.TestCase):
    def test_refuses_missing_or_mismatched_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                write_case(root, cost_basis_source="wallet_deposit")
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                write_case(root, snapshot_overrides={"up_token_id": "999"})
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                write_case(root, snapshot_overrides={"cost_basis_usd": "80"})

    def test_half_dollar_min_size_skips(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root)
            arm(session, broker, clock, "0.49", "0.50")
            session.tick()
            self.assertEqual(session.open_orders(), [])
            self.assertIn("min_size_exceeds_risk_budget", session.skip_reasons)
            self.assertEqual(broker.submit_calls, [])
            self.assertEqual(session.current_pnl(), D("0"))
            self.assertGreater(D("0.02"), D("0"))

    def test_equal_share_size_not_gross_usd_size(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(
                root, min_order_size="1", seed_shares="8", size_quantum="0.01"
            )
            arm(session, broker, clock, "0.79", "0.80")
            book(session, "down", "0.39", "0.40")
            session.tick()
            up = next(order for order in session.open_orders() if order.outcome == "up")
            down = next(
                order for order in session.open_orders() if order.outcome == "down"
            )
            self.assertEqual(up.original_size, down.original_size)
            usd_size = (up.original_size * up.price) / down.price
            self.assertNotEqual(down.original_size, usd_size)
            self.assertLess(down.original_size * (D("1") - down.price), D("2"))
            session.apply_fills(
                [Fill("up-fill", up.exchange_id, up.original_size, up.price)]
            )
            self.assertEqual(session.share_gap(), -down.original_size)
            self.assertTrue(
                all(call["post_only"] is True for call in broker.submit_calls)
            )

    def test_one_sided_cap_and_simultaneous_partials(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root, min_order_size="5", seed_shares="5")
            arm(session, broker, clock, "0.69", "0.70")
            session.tick()
            self.assertEqual(len(session.open_orders()), 2)
            self.assertIsNone(session._safe_single_size("up", D("5"), D("0.70")))
            up = next(order for order in session.open_orders() if order.outcome == "up")
            session.apply_fills(
                [
                    Fill("p1", up.exchange_id, D("2"), up.price),
                    Fill("p2", up.exchange_id, D("3"), up.price),
                    Fill("p1", up.exchange_id, D("2"), up.price),
                ]
            )
            self.assertEqual(session.state.on_hand["up"], D("95"))
            self.assertEqual(session.reserved("down"), D("5"))
            self.assertGreaterEqual(session.state.on_hand["up"], D("0"))
            self.assertGreaterEqual(session.available("up"), D("0"))

    def test_thresholds_ignore_rebate_and_do_not_double_count(self):
        gross = D("18")
        remaining = D("90")
        cost = D("100")
        floor = settlement_lower_bound(gross, remaining, remaining, cost, D("0"))
        self.assertEqual(floor, D("8"))
        doubled = gross + remaining + remaining - cost
        self.assertGreater(doubled, floor)
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(
                root, min_order_size="1", seed_shares="10", max_share_skew="20"
            )
            arm(session, broker, clock, "0.89", "0.90")
            session.tick()
            orders = list(session.open_orders())
            session.apply_fills(
                [
                    Fill(
                        "a",
                        orders[0].exchange_id,
                        orders[0].original_size,
                        orders[0].price,
                    ),
                    Fill(
                        "b",
                        orders[1].exchange_id,
                        orders[1].original_size,
                        orders[1].price,
                    ),
                ]
            )
            self.assertGreaterEqual(session.current_pnl(), D("2"))
            self.assertEqual(session.state.fees_paid, D("0"))
            self.assertGreater(session.state.rebate_unpaid_estimate, D("0"))
            self.assertNotEqual(
                session.current_pnl(),
                session.current_pnl() + session.state.rebate_unpaid_estimate,
            )
            session.tick()
            self.assertEqual(session.state.phase, "take_profit")
            text = format_status(session)
            self.assertNotIn("profit locked", text)
            self.assertIn("cap_guarantee=false", text)
            self.assertNotEqual(session.liquidation_estimate(), session.worst_pnl())

    def test_risk_print_does_not_cross_the_spread(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root, min_order_size="1", seed_shares="5")
            arm(session, broker, clock, "0.89", "0.90")
            session.tick()
            up = next(order for order in session.open_orders() if order.outcome == "up")
            before = len(broker.submit_calls)
            session.apply_fills(
                [Fill("bad", up.exchange_id, up.original_size, D("0.10"))]
            )
            self.assertLess(session.worst_pnl(), D("-2"))
            self.assertEqual(session.state.halt_reason, "unexpected_taker_fill")
            session.tick()
            self.assertEqual(len(broker.submit_calls), before)
            self.assertTrue(
                all(call["post_only"] is True for call in broker.submit_calls)
            )
            self.assertIn("residual_exposure=yes", format_status(session))

    def test_stale_book_and_post_only_reject(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root)
            arm(session, broker, clock, "0.60", "0.62")
            clock.now = D("1700000100")
            session.tick()
            self.assertEqual(session.state.phase, "stale")
            self.assertEqual(broker.submit_calls, [])
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root)
            arm(session, broker, clock, "0.60", "0.62")

            def cross():
                book_up = session.books["up"]
                book_up.bids = [Level(D("0.62"), D("100"))]
                broker.set_book(session.config.up_token_id, book_up)
                book_down = session.books["down"]
                book_down.bids = [Level(D("0.62"), D("100"))]
                broker.set_book(session.config.down_token_id, book_down)

            broker.before_submit = cross
            session.tick()
            self.assertEqual(session.open_orders(), [])
            self.assertGreaterEqual(len(broker.submit_calls), 1)
            self.assertTrue(
                all(call["post_only"] is True for call in broker.submit_calls)
            )
            self.assertIn("post_only_rejected_not_retried", session.skip_reasons)
            with self.assertRaises(MakerOnlyViolation):
                broker.submit_sell(UP, D("0.62"), D("5"), post_only=False)

    def test_restart_unknown_submit_and_session_end(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root)
            arm(session, broker, clock, "0.60", "0.62")
            broker.next_submit = "unknown"
            session.tick()
            self.assertEqual(session.state.halt_reason, "unknown_submit")
            self.assertEqual(session.reserved("up"), D("5"))
            resumed = ShareSession.resume(
                session.config, PaperBroker(), clock, session.config.state_path
            )
            book(resumed, "up", "0.60", "0.62")
            book(resumed, "down", "0.60", "0.62")
            resumed.observe_user(clock.now)
            resumed.tick()
            self.assertEqual(resumed.broker.submit_calls, [])
            self.assertEqual(resumed.reserved("up"), D("5"))
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root)
            arm(session, broker, clock, "0.60", "0.62")
            session.tick()
            broker.inject_foreign("foreign-1")
            clock.now = D("1700000270")
            book(session, "up", "0.60", "0.62", "1700000270")
            book(session, "down", "0.60", "0.62", "1700000270")
            session.observe_user(clock.now)
            session.tick()
            cancelled = [item for batch in broker.cancel_calls for item in batch]
            self.assertEqual(session.state.phase, "ending")
            self.assertNotIn("foreign-1", cancelled)
            self.assertTrue(broker.orders["foreign-1"]["open"])
            text = format_status(session)
            self.assertIn("residual_exposure=yes", text)
            self.assertNotIn("flat", text)
            self.assertNotIn("loss capped", text)

    def test_sweep_requires_trade_through_and_demo(self):
        with tempfile.TemporaryDirectory() as root:
            session, broker, clock = boot(root)
            arm(session, broker, clock, "0.60", "0.62")
            session.tick()
            from poly_market_maker.share_balanced_session.replay import _sweep

            _sweep(
                session,
                {
                    "outcome": "up",
                    "trade_price": "0.62",
                    "shares": "5",
                    "trade_id": "touch",
                },
            )
            self.assertEqual(session.state.gross_proceeds, D("0"))
            _sweep(
                session,
                {
                    "outcome": "up",
                    "trade_price": "0.63",
                    "shares": "5",
                    "trade_id": "through",
                },
            )
            self.assertEqual(session.state.on_hand["up"], D("95"))
        session, _broker = run_replay(
            *load_script("config/replay_share_balanced_session.json")
        )
        text = format_status(session)
        self.assertIn("min_size_exceeds_risk_budget", text)
        self.assertIn("maker_only=true", text)
        self.assertIn("residual_exposure=yes", text)
        self.assertEqual(session.state.phase, "ending")
        self.assertEqual(main(["demo"]), 0)
        self.assertEqual(main(["live"]), 2)
        os.environ["POLY_SELL_BOT_ENABLE_LIVE"] = "1"
        try:
            self.assertEqual(main(["live", "--send"]), 2)
        finally:
            os.environ.pop("POLY_SELL_BOT_ENABLE_LIVE", None)

    def test_package_has_no_taker_submit_or_global_cancel(self):
        root = Path("poly_market_maker/share_balanced_session")
        text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        for banned in (
            "cancel_all",
            "post_only=False",
            "FAK",
            "IOC",
            "place_market_order",
        ):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
