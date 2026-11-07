import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.config import SellConfig, load_config
from poly_market_maker.sell_equal.engine import Fill, MarketView, SellEqualEngine
from poly_market_maker.sell_equal.events import parse_user_payload
from poly_market_maker.sell_equal.live import (
    LiveBroker,
    LiveDisabled,
    cap_inventory,
    interpret_cancel_response,
    interpret_order_response,
    live_opt_in_enabled,
    run_prepared_cycle,
    run_user_stream,
    spender_for,
)
from poly_market_maker.sell_equal.paper import PaperBroker
from poly_market_maker.sell_equal.quant import D, shares_for_gross
from poly_market_maker.sell_equal.replay import load_script, run_replay
from poly_market_maker.sell_equal.verify import verify_public_market
from poly_market_maker.sell_equal.__main__ import main


UP = "44857089090901365835706371888611204979538894171442730872794600452065181330944"
DOWN = "17424551638207318915606858171665237870653355896978276914963148187077601073259"
CONDITION = "0xe4966d642938113525dfb125060bda90b9f8d8c25945ae352de1b75c160bc383"


class Clock:
    def __init__(self, now="1700000000"):
        self.now = D(now)

    def __call__(self):
        return self.now


def make_config(root, **overrides):
    raw = {
        "mode": "paper",
        "condition_id": CONDITION,
        "slug": "btc-updown-5m-1794024900",
        "up_token_id": UP,
        "down_token_id": DOWN,
        "up_outcome": "Up",
        "down_outcome": "Down",
        "tick_size": "0.01",
        "min_order_size": "5",
        "size_quantum": "0.01",
        "price_floor": "0.01",
        "price_ceiling": "0.99",
        "max_reprice_ticks": 0,
        "seed_price_min": "0.60",
        "seed_price_max": "0.60",
        "seed_price_step": "0.01",
        "seed_shares": "10",
        "hedge_price": {"up": "0.40", "down": "0.40"},
        "inventory": {"up": "100", "down": "100"},
        "mark_price": {"up": "0.50", "down": "0.50"},
        "max_outstanding_notional": "500",
        "max_gross_skew": "500",
        "max_session_loss": "1000",
        "value_dust": "0.01",
        "stale_after_seconds": "30",
        "market_end_ts": "2000000000",
        "share_decimals": 6,
        "require_user_feed": True,
        "state_path": str(Path(root) / "state.json"),
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key].update(value)
        else:
            raw[key] = value
    path = Path(root) / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(path)


class Harness:
    def __init__(self, root, **overrides):
        self.clock = Clock()
        self.config = make_config(root, **overrides)
        self.broker = PaperBroker()
        self.engine = SellEqualEngine(self.config, self.broker, self.clock)

    def open_market(self):
        self.engine.observe_market(
            MarketView(
                ts=self.clock.now,
                accepting=True,
                ended=False,
                tick_size=self.config.tick_size,
                min_order_size=self.config.min_order_size,
            )
        )
        self.engine.observe_user(self.clock.now)
        self.engine.tick()

    def fill(self, outcome, price, shares, fill_id):
        price_d = D(price)
        matches = [
            order
            for order in self.engine.state.orders
            if order.outcome == outcome
            and order.price == price_d
            and order.exchange_id
            and order.status == "open"
        ]
        self.assert_one(matches)
        self.engine.apply_fills(
            [
                Fill(
                    fill_id=fill_id,
                    exchange_order_id=matches[0].exchange_id,
                    shares=D(shares),
                    price=price_d,
                )
            ]
        )
        return matches[0]

    def hedges(self):
        return [order for order in self.engine.open_orders() if order.role == "hedge"]

    @staticmethod
    def assert_one(rows):
        if len(rows) != 1:
            raise AssertionError("expected 1 order, found %s" % len(rows))


class _Page:
    def __init__(self, rows):
        self.rows = rows

    def iter_items(self):
        return iter(self.rows)


class _Balance:
    def __init__(self):
        self.balance = 100 * 10**6
        self.allowances = {"0xE111180000d2663C0091e4f400237545B87B996B": 100 * 10**6}


class _CycleClient:
    def __init__(self):
        self.orders = []
        self.asset_type = None

    def get_balance_allowance(self, **kwargs):
        self.asset_type = kwargs["asset_type"]
        return _Balance()

    def create_limit_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"signed": True, "side": kwargs["side"]}

    def post_order(self, order):
        return type(
            "Accepted",
            (),
            {"ok": True, "order_id": "live-%s" % len(self.orders)},
        )()

    def list_open_orders(self, market):
        rows = []
        for index, _order in enumerate(self.orders, start=1):
            rows.append(
                type(
                    "Row",
                    (),
                    {
                        "id": "live-%s" % index,
                        "market": market,
                        "size_matched": D("0"),
                    },
                )()
            )
        return _Page(rows)

    def list_account_trades(self, market):
        del market
        return _Page([])


def _snapshot_fetch(url):
    snapshot = json.loads(
        Path("config/verified_btc_updown_snapshot.json").read_text(encoding="utf-8")
    )
    if "/clob-markets/" in url:
        return snapshot["clob_market"]
    if "gamma-api" in url:
        return [snapshot["gamma_market"]]
    if UP in url:
        return snapshot["up_book"]
    if DOWN in url:
        return snapshot["down_book"]
    raise AssertionError(url)


class EqualValueTests(unittest.TestCase):
    def test_equal_gross_not_equal_shares(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            harness.fill("up", "0.60", "10", "fill-1")
            hedges = harness.hedges()
            self.assertEqual(len(hedges), 1)
            self.assertEqual(hedges[0].outcome, "down")
            self.assertEqual(hedges[0].original_size, D("15"))
            self.assertEqual(hedges[0].price, D("0.40"))
            self.assertEqual(hedges[0].original_size * hedges[0].price, D("6"))
            self.assertNotEqual(hedges[0].original_size, D("10"))
            self.assertEqual(harness.engine.state.gross_sold["up"], D("6"))
            self.assertEqual(harness.engine.state.value_basis, "gross")

    def test_partial_fill_and_dedup(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            order = harness.fill("up", "0.60", "4", "partial-1")
            hedge = harness.hedges()[0]
            self.assertEqual(hedge.original_size, D("6"))
            self.assertEqual(hedge.original_size * hedge.price, D("2.40"))
            self.assertEqual(harness.engine.state.on_hand["up"], D("96"))
            self.assertEqual(order.reserved_size, D("6"))
            before = len(harness.broker.submit_calls)
            harness.engine.apply_fills(
                [
                    Fill(
                        fill_id="partial-1",
                        exchange_order_id=order.exchange_id,
                        shares=D("4"),
                        price=D("0.60"),
                    )
                ]
            )
            self.assertEqual(harness.engine.state.gross_sold["up"], D("2.40"))
            self.assertEqual(len(harness.broker.submit_calls), before)

    def test_simultaneous_opposite_seeds_net(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            up = next(
                order for order in harness.engine.open_orders() if order.outcome == "up"
            )
            down = next(
                order
                for order in harness.engine.open_orders()
                if order.outcome == "down" and order.role == "seed"
            )
            harness.engine.apply_fills(
                [
                    Fill("both-up", up.exchange_id, D("10"), D("0.60")),
                    Fill("both-down", down.exchange_id, D("10"), D("0.60")),
                ]
            )
            self.assertEqual(harness.hedges(), [])
            self.assertEqual(harness.engine.state.gross_sold["up"], D("6"))
            self.assertEqual(harness.engine.state.gross_sold["down"], D("6"))

    def test_counterpart_fill_cancels_hedge_without_reverse(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            submits_after_seeds = len(harness.broker.submit_calls)
            harness.fill("up", "0.60", "10", "up-1")
            self.assertEqual(len(harness.hedges()), 1)
            harness.fill("down", "0.60", "10", "down-seed")
            self.assertEqual(harness.hedges(), [])
            self.assertEqual(len(harness.broker.submit_calls), submits_after_seeds + 1)
            self.assertFalse(
                any(
                    call[0] == UP and call[1] == D("0.40")
                    for call in harness.broker.submit_calls
                )
            )

    def test_hedge_fill_is_not_recursive(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            harness.fill("up", "0.60", "10", "up-1")
            before = len(harness.broker.submit_calls)
            harness.fill("down", "0.40", "15", "hedge-1")
            self.assertEqual(harness.engine.state.gross_sold["down"], D("6"))
            self.assertEqual(harness.hedges(), [])
            self.assertEqual(len(harness.broker.submit_calls), before)

    def test_reservation_blocks_oversell(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                inventory={"up": "12", "down": "12"},
                seed_price_min="0.51",
                seed_price_max="0.52",
            )
            harness.open_market()
            self.assertEqual(
                len(
                    [
                        order
                        for order in harness.engine.open_orders()
                        if order.outcome == "up"
                    ]
                ),
                1,
            )
            self.assertEqual(harness.engine.available("up"), D("2"))
            harness.fill("up", "0.51", "10", "sold")
            self.assertEqual(harness.hedges(), [])
            self.assertGreaterEqual(harness.engine.state.on_hand["up"], D("0"))
            self.assertGreaterEqual(harness.engine.available("down"), D("0"))

    def test_dust_min_size_and_tick(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            harness.fill("up", "0.60", "2", "dust")
            self.assertEqual(harness.hedges(), [])
            self.assertEqual(harness.engine.state.gross_sold["up"], D("1.20"))
            self.assertIsNone(
                shares_for_gross(D("1.20"), D("0.40"), D("0.01"), D("5"), D("100"))
            )
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, seed_price_min="0.605", seed_price_max="0.605")
            harness.open_market()
            self.assertEqual(harness.engine.open_orders(), [])
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, hedge_price={"up": "0.001", "down": "0.001"})
            harness.open_market()
            harness.fill("up", "0.60", "10", "nofloor")
            self.assertEqual(harness.hedges(), [])

    def test_restart_dedups_and_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            order = harness.fill("up", "0.60", "4", "partial-1")
            resumed = SellEqualEngine.resume(
                harness.config, PaperBroker(), harness.clock, harness.config.state_path
            )
            resumed.apply_fills(
                [
                    Fill(
                        fill_id="partial-1",
                        exchange_order_id=order.exchange_id,
                        shares=D("4"),
                        price=D("0.60"),
                    )
                ]
            )
            self.assertEqual(resumed.state.gross_sold["up"], D("2.40"))
            restored = next(
                item
                for item in resumed.state.orders
                if item.exchange_id == order.exchange_id
            )
            self.assertEqual(restored.reserved_size, D("6"))
            self.assertEqual(resumed.state.on_hand["up"], D("96"))

    def test_unknown_submit_and_cancel(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.broker.next_submit = "unknown"
            harness.open_market()
            self.assertEqual(harness.engine.state.halt_reason, "unknown_submit")
            self.assertEqual(len(harness.broker.submit_calls), 1)
            self.assertEqual(harness.engine.available("up"), D("90"))
            resumed = SellEqualEngine.resume(
                harness.config, PaperBroker(), harness.clock, harness.config.state_path
            )
            resumed.observe_market(
                MarketView(
                    ts=harness.clock.now,
                    accepting=True,
                    ended=False,
                    tick_size=harness.config.tick_size,
                    min_order_size=harness.config.min_order_size,
                )
            )
            resumed.observe_user(harness.clock.now)
            resumed.tick()
            self.assertEqual(resumed.broker.submit_calls, [])

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.broker.next_submit = "unknown_with_id"
            harness.open_market()
            order = harness.engine.state.orders[0]
            harness.engine.reconcile(
                {order.exchange_id},
                {order.exchange_id: D("0")},
                True,
                harness.clock.now,
            )
            self.assertEqual(order.status, "open")
            self.assertIsNone(harness.engine.state.halt_reason)

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            harness.fill("up", "0.60", "10", "up-1")
            harness.broker.next_cancel = "unknown"
            harness.fill("down", "0.60", "10", "down-1")
            self.assertEqual(harness.engine.state.halt_reason, "unknown_cancel")
            hedge = next(
                order for order in harness.engine.state.orders if order.role == "hedge"
            )
            self.assertEqual(hedge.reserved_size, D("15"))
            self.assertEqual(hedge.status, "unknown_cancel")
            submits = len(harness.broker.submit_calls)
            harness.engine.tick()
            self.assertEqual(len(harness.broker.submit_calls), submits)

    def test_unexplained_absence_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            order = harness.engine.open_orders()[0]
            harness.engine.reconcile(
                set(), {order.exchange_id: D("0")}, True, harness.clock.now
            )
            self.assertEqual(harness.engine.state.halt_reason, "unexplained_absence")
            self.assertEqual(order.reserved_size, D("10"))

    def test_stale_and_market_end_and_shutdown(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            submits = len(harness.broker.submit_calls)
            harness.clock.now = D("1700000100")
            harness.engine.tick()
            self.assertEqual(harness.engine.state.halt_reason, "stale_market")
            self.assertEqual(len(harness.broker.submit_calls), submits)

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            harness.broker.inject_foreign("foreign-1")
            harness.engine.observe_market(
                MarketView(
                    ts=harness.clock.now,
                    accepting=False,
                    ended=True,
                    tick_size=harness.config.tick_size,
                    min_order_size=harness.config.min_order_size,
                )
            )
            harness.engine.tick()
            self.assertEqual(harness.engine.state.halt_reason, "market_ended")
            cancelled = [
                item for batch in harness.broker.cancel_calls for item in batch
            ]
            self.assertNotIn("foreign-1", cancelled)
            self.assertTrue(harness.broker.orders["foreign-1"]["open"])
            self.assertTrue(cancelled)

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            harness.broker.inject_foreign("foreign-2")
            own = {order.exchange_id for order in harness.engine.open_orders()}
            harness.engine.shutdown()
            cancelled = [
                item for batch in harness.broker.cancel_calls for item in batch
            ]
            self.assertEqual(set(cancelled), own)
            self.assertNotIn("foreign-2", cancelled)
            self.assertTrue(harness.broker.orders["foreign-2"]["open"])

    def test_session_loss_and_notional_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                seed_price_min="0.40",
                seed_price_max="0.40",
                max_session_loss="0.50",
            )
            harness.open_market()
            harness.fill("up", "0.40", "10", "cheap")
            self.assertEqual(harness.engine.state.halt_reason, "session_loss")
            self.assertEqual(harness.engine.session_loss(), D("1.0"))

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, max_outstanding_notional="5")
            harness.open_market()
            self.assertEqual(harness.engine.open_orders(), [])

    def test_user_stream_partial_dedup_and_failure(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.open_market()
            order = next(
                item for item in harness.engine.open_orders() if item.outcome == "up"
            )
            payload = {
                "event_type": "trade",
                "id": "trade-1",
                "status": "MATCHED",
                "trader_side": "MAKER",
                "taker_order_id": "someone-else",
                "side": "BUY",
                "size": "10",
                "price": "0.60",
                "maker_orders": [
                    {
                        "order_id": order.exchange_id,
                        "side": "SELL",
                        "matched_amount": "4",
                        "price": "0.60",
                        "fee_rate_bps": "1000",
                    }
                ],
            }
            batch = parse_user_payload(payload, {order.exchange_id})
            harness.engine.consume_user_batch(batch)
            harness.engine.consume_user_batch(batch)
            self.assertEqual(harness.engine.state.gross_sold["up"], D("2.40"))
            update = {
                "event_type": "order",
                "type": "UPDATE",
                "id": order.exchange_id,
                "side": "SELL",
                "size_matched": "4",
                "price": "0.60",
            }
            harness.engine.consume_user_batch(
                parse_user_payload(update, {order.exchange_id})
            )
            self.assertEqual(harness.engine.state.gross_sold["up"], D("2.40"))
            failed = dict(payload)
            failed["id"] = "trade-2"
            failed["status"] = "FAILED"
            harness.engine.consume_user_batch(
                parse_user_payload(failed, {order.exchange_id})
            )
            self.assertEqual(harness.engine.state.halt_reason, "fill_failed")
            self.assertEqual(harness.engine.state.gross_sold["up"], D("2.40"))

    def test_buy_message_is_ignored(self):
        payload = {
            "event_type": "trade",
            "id": "buy-1",
            "status": "MATCHED",
            "trader_side": "TAKER",
            "taker_order_id": "our-buy",
            "side": "BUY",
            "size": "10",
            "price": "0.40",
            "maker_orders": [],
        }
        batch = parse_user_payload(payload, {"our-buy"})
        self.assertEqual(batch.fills, ())

    def test_replay_demo_and_cli(self):
        _engine, _broker, checks = run_replay(
            *load_script("config/replay_equal_usd.json")
        )
        self.assertEqual(len(checks), 1)
        self.assertEqual(main(["demo"]), 0)
        self.assertEqual(main(["live"]), 2)

    def test_snapshot_uses_gamma_end_not_legacy_midnight(self):
        snapshot = json.loads(
            Path("config/verified_btc_updown_snapshot.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root, stale_after_seconds="600")
        fresh = datetime.fromtimestamp(1794025100, tz=timezone.utc)
        self.assertEqual(
            verify_public_market(
                config,
                snapshot["clob_market"],
                snapshot["gamma_market"],
                snapshot["up_book"],
                snapshot["down_book"],
                fresh,
            ),
            [],
        )
        ended = datetime.fromtimestamp(1794025200, tz=timezone.utc)
        errors = verify_public_market(
            config,
            snapshot["clob_market"],
            snapshot["gamma_market"],
            snapshot["up_book"],
            snapshot["down_book"],
            ended,
        )
        self.assertIn("market endDate has passed", errors)
        self.assertEqual(
            snapshot["gamma_market"]["end_date_iso"], "2026-11-07T00:00:00Z"
        )

    def test_live_adapter_is_sell_only_and_opt_in(self):
        self.assertFalse(live_opt_in_enabled())
        self.assertEqual(
            spender_for("v1", False), "0xE111180000d2663C0091e4f400237545B87B996B"
        )
        source = Path("poly_market_maker/sell_equal/live.py").read_text(
            encoding="utf-8"
        )
        for banned in (
            "cancel_all",
            "cancel_market_orders",
            "place_limit_order",
            "place_market_order",
            "approve_erc20",
            "approve_erc1155",
            "update_balance_allowance",
            "merge_positions",
        ):
            self.assertNotIn(banned, source)

        class Response:
            def __init__(self, ok, order_id=None):
                self.ok = ok
                self.order_id = order_id

        self.assertEqual(
            interpret_order_response(Response(True, "abc")).status, "accepted"
        )
        self.assertEqual(interpret_order_response(Response(False)).status, "rejected")
        with self.assertRaises(LiveDisabled):
            cap_inventory(D("100"), D("0"), D("100"), D("5"))

        class Cancelled:
            canceled = ("paper-1",)
            not_canceled = {}

        result = interpret_cancel_response(Cancelled(), ["paper-1"])
        self.assertEqual(result.status, "known")

    def test_prepared_cycle_rejects_bad_market_without_orders(self):
        calls = []

        class Client:
            def create_limit_order(self, **kwargs):
                calls.append(kwargs)
                raise AssertionError("should not sign")

            def get_balance_allowance(self, **kwargs):
                calls.append(kwargs)
                raise AssertionError("should not read balances before verification")

        with tempfile.TemporaryDirectory() as root:
            config = make_config(root)
            os.environ["POLY_SELL_BOT_ENABLE_LIVE"] = "1"
            try:
                with self.assertRaises(LiveDisabled):
                    run_prepared_cycle(
                        config,
                        Client(),
                        lambda url: [],
                        datetime.fromtimestamp(1794025100, tz=timezone.utc),
                    )
            finally:
                os.environ.pop("POLY_SELL_BOT_ENABLE_LIVE", None)
        self.assertEqual(calls, [])

    def test_prepared_cycle_posts_sell_without_approval_helper(self):
        client = _CycleClient()
        with tempfile.TemporaryDirectory() as root:
            config = make_config(
                root,
                mode="live",
                stale_after_seconds="600",
                market_end_ts="2000000000",
                seed_shares="10",
            )
            os.environ["POLY_SELL_BOT_ENABLE_LIVE"] = "1"
            try:
                engine = run_prepared_cycle(
                    config,
                    client,
                    _snapshot_fetch,
                    datetime.fromtimestamp(1794025100, tz=timezone.utc),
                )
            finally:
                os.environ.pop("POLY_SELL_BOT_ENABLE_LIVE", None)
        self.assertEqual(client.asset_type, "CONDITIONAL")
        self.assertGreaterEqual(len(client.orders), 1)
        self.assertTrue(all(item["side"] == "SELL" for item in client.orders))
        self.assertTrue(all(item["post_only"] is True for item in client.orders))
        self.assertIsNone(engine.state.halt_reason)
        self._assert_own_cancel(client)

    def _assert_own_cancel(self, client):
        broker = LiveBroker(client, CONDITION, {UP, DOWN})
        seen = []

        def cancel_orders(order_ids):
            seen.append(list(order_ids))
            return type(
                "Cancel", (), {"canceled": tuple(order_ids), "not_canceled": {}}
            )()

        client.cancel_orders = cancel_orders
        os.environ["POLY_SELL_BOT_ENABLE_LIVE"] = "1"
        try:
            result = broker.cancel(["live-1"])
        finally:
            os.environ.pop("POLY_SELL_BOT_ENABLE_LIVE", None)
        self.assertEqual(result.canceled_ids, frozenset({"live-1"}))
        self.assertEqual(seen, [["live-1"]])

    def test_user_stream_helper_requires_second_flag(self):
        with self.assertRaises(LiveDisabled):
            run_user_stream(lambda markets: markets, CONDITION, lambda message: None)

    def test_config_rejects_numbers(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.json"
            raw = json.loads(
                Path("config/sell_equal_demo.json").read_text(encoding="utf-8")
            )
            raw["tick_size"] = 0.01
            raw["state_path"] = str(path) + ".state"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
