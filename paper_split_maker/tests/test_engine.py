"""Offline scenarios for the paper ledger. None of these cap live losses."""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path

import pytest

from paper_split_maker.config import Config, ConfigError, config_from_mapping
from paper_split_maker.engine import Engine
from paper_split_maker.models import State
from paper_split_maker.risk import sell_worst_term, worst_case_equity
from tests.helpers import both_books, book_event, make_engine, market_event, run

ROOT = Path(__file__).resolve().parents[1]


def _kinds(logs, kind):
    return [row for row in logs if row["kind"] == kind]


def test_worst_case_formula_pins_inventory_orders_and_fee_reserve(tmp_path):
    engine = make_engine(
        tmp_path,
        risk_fee_rate=Decimal("0.07"),
        operational_reserve=Decimal("0.05"),
        global_rounding_reserve=Decimal("0.01"),
        auto_split=False,
    )
    assert worst_case_equity(engine.state) == Decimal("999.99")
    run(engine, [market_event()])
    assert worst_case_equity(engine.state) == Decimal("999.94")
    assert engine._split(engine.state.markets["m1"], Decimal("20"), "test")
    order_up = engine.state.orders
    assert order_up == {}
    from paper_split_maker.models import Order

    engine.state.orders["o1"] = Order(
        order_id="o1",
        market_id="m1",
        outcome="up",
        price=Decimal("0.52"),
        size=Decimal("5"),
        remaining=Decimal("5"),
        queue_ahead=Decimal("0"),
        status="queued",
        submit_ts=0,
    )
    assert sell_worst_term(Decimal("5"), Decimal("0.52"), Decimal("0.07")) == Decimal(
        "-2.48736"
    )
    assert worst_case_equity(engine.state) == Decimal("997.45264")
    engine.assert_invariants()


def test_symmetric_demo_scenario_is_flat_to_slightly_positive(tmp_path):
    config = config_from_mapping(
        __import__("json").loads(
            (ROOT / "config.paper.json").read_text(encoding="utf-8")
        )
    )
    from paper_split_maker.session import run_demo

    summary = run_demo(
        config,
        tmp_path / "out",
        ROOT / "tests" / "fixtures" / "symmetric_demo.json",
    )
    assert summary["live_orders"] is False
    assert Decimal(summary["cash"]) == Decimal("1000.20")
    assert Decimal(summary["realized_pnl"]) == Decimal("0.20")
    assert Decimal(summary["worst_terminal_equity"]) >= Decimal(summary["loss_floor"])
    assert Decimal(summary["rebate_estimate_unguaranteed"]) == Decimal("0.034944")
    assert summary["rebate_included_in_pnl"] is False
    assert summary["rebate_included_in_risk_floor"] is False
    market = summary["markets"][0]
    assert market["status"] == "redeemed"
    assert market["winner"] == "up"
    assert market["resolution_source"] == "fixture"
    assert market["synthetic"] is True


def test_price_touch_does_not_fill_and_queue_must_clear_first(tmp_path):
    engine = make_engine(tmp_path, improve_queue_shares=Decimal("0"))
    logs = run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.48", "0.52", ts=0, ask_size="10"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
        ],
    )
    assert _kinds(logs, "quote_submit")
    assert engine.state.markets["m1"].up == Decimal("20")
    # Touch the ask with the bid. No trade print, so no fill.
    run(
        engine,
        [book_event("m1", "up", "0.52", "0.53", ts=10, digest="touch")],
    )
    assert engine.state.markets["m1"].up == Decimal("20")
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "t-queue",
                "ts": 20,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "10",
                "taker_side": "BUY",
                "trade_id": "queue-only",
            }
        ],
    )
    assert engine.state.markets["m1"].up == Decimal("20")
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "t-fill",
                "ts": 21,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "5",
                "taker_side": "BUY",
                "trade_id": "fill-up",
            }
        ],
    )
    assert engine.state.markets["m1"].up == Decimal("15")
    # A later duplicate print must not fill again.
    before = engine.state.cash
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "t-fill-again",
                "ts": 22,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "5",
                "taker_side": "BUY",
                "trade_id": "fill-up",
            }
        ],
    )
    assert engine.state.cash == before


def test_extreme_book_and_reversal_do_not_invent_a_winner_or_cross(tmp_path):
    engine = make_engine(tmp_path, min_edge=Decimal("0.01"), merge_when_idle=False)
    run(
        engine,
        [
            market_event(end_ts=50_000),
            *both_books("m1", "0.98", "0.99", ts=0),
        ],
    )
    # Down is the cheap book; replace it with a real 0.01/0.02 book.
    run(
        engine,
        [
            book_event("m1", "down", "0.01", "0.02", ts=1, digest="down-cheap"),
            {"type": "heartbeat", "ts": 1, "event_id": "hb1"},
        ],
    )
    for order in engine.state.orders.values():
        assert order.price > Decimal("0.01")
        book = engine.state.markets["m1"].book(order.outcome)
        assert order.price > book.best_bid()
    assert engine.state.markets["m1"].winner is None
    # Sudden reversal of the public books. Still no resolution.
    run(
        engine,
        [
            book_event("m1", "up", "0.01", "0.02", ts=2, digest="up-reversed"),
            book_event("m1", "down", "0.98", "0.99", ts=2, digest="down-reversed"),
            {"type": "heartbeat", "ts": 2, "event_id": "hb2"},
        ],
    )
    assert engine.state.markets["m1"].status == "open"
    assert engine.state.markets["m1"].winner is None
    for order in engine.state.orders.values():
        if order.status in {"queued", "live", "cancel_requested"}:
            assert order.price >= Decimal("0.03")
    run(
        engine,
        [
            {"type": "clock", "ts": 50_000, "event_id": "expire"},
            {"type": "heartbeat", "ts": 50_000, "event_id": "hb-exp"},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.status == "expired_unresolved"
    assert market.winner is None
    assert market.up == market.down
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_only_one_side_can_fill(tmp_path):
    engine = make_engine(tmp_path, fill_mode="none")
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.42", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
        ],
    )
    assert (
        len([order for order in engine.state.orders.values() if order.outcome == "up"])
        == 1
    )
    assert (
        len(
            [order for order in engine.state.orders.values() if order.outcome == "down"]
        )
        == 1
    )
    up = next(order for order in engine.state.orders.values() if order.outcome == "up")
    before = worst_case_equity(engine.state)
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "adv-up",
                "ts": 5,
                "order_id": up.order_id,
                "size": "5",
                "label": "synthetic",
            }
        ],
    )
    market = engine.state.markets["m1"]
    assert market.up == Decimal("15")
    assert market.down == Decimal("20")
    down = next(
        order for order in engine.state.orders.values() if order.outcome == "down"
    )
    assert down.status != "filled"
    assert worst_case_equity(engine.state) >= before
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_simultaneous_pending_fills_were_reserved_before_the_print(tmp_path):
    engine = make_engine(tmp_path)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.42", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
        ],
    )
    reserved_worst = worst_case_equity(engine.state)
    orders = list(engine.state.orders.values())
    assert len(orders) == 2
    assert all(order.status == "queued" for order in orders)
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "a",
                "ts": 1,
                "order_id": orders[0].order_id,
                "size": str(orders[0].remaining),
                "label": "synthetic",
            },
            {
                "type": "adverse_fill",
                "event_id": "b",
                "ts": 1,
                "order_id": orders[1].order_id,
                "size": str(orders[1].remaining),
                "label": "synthetic",
            },
        ],
    )
    assert worst_case_equity(engine.state) >= reserved_worst
    assert engine.state.markets["m1"].up == Decimal("15")
    assert engine.state.markets["m1"].down == Decimal("15")


def test_cancel_latency_still_allows_a_fill_and_disconnect_holds_the_cancel(tmp_path):
    engine = make_engine(tmp_path, ack_latency_ms=100, cancel_latency_ms=500)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.42", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
            {"type": "clock", "ts": 100, "event_id": "live"},
        ],
    )
    up = next(order for order in engine.state.orders.values() if order.outcome == "up")
    assert up.status == "live"
    run(
        engine,
        [{"type": "cancel", "event_id": "c1", "ts": 150, "order_id": up.order_id}],
    )
    assert up.status == "cancel_requested"
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "late-fill",
                "ts": 400,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.42",
                "size": "6",
                "taker_side": "BUY",
                "trade_id": "late",
            }
        ],
    )
    assert up.status == "filled"
    down = next(
        order for order in engine.state.orders.values() if order.outcome == "down"
    )
    run(
        engine,
        [{"type": "cancel", "event_id": "c2", "ts": 420, "order_id": down.order_id}],
    )
    run(engine, [{"type": "feed", "event_id": "down", "ts": 430, "status": "down"}])
    run(engine, [{"type": "clock", "event_id": "wait", "ts": 5000}])
    assert down.status == "cancel_requested"
    run(engine, [{"type": "feed", "event_id": "up", "ts": 5100, "status": "up"}])
    assert down.status == "cancel_requested"
    run(engine, [{"type": "clock", "event_id": "confirm", "ts": 5600}])
    assert down.status == "cancelled"
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_stale_feed_stops_new_quotes_but_keeps_reservation_until_cancel(tmp_path):
    engine = make_engine(
        tmp_path, stale_after_ms=1000, ack_latency_ms=0, cancel_latency_ms=300
    )
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.48", "0.52", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
            {"type": "clock", "ts": 1, "event_id": "live"},
        ],
    )
    assert any(order.status == "live" for order in engine.state.orders.values())
    logs = run(engine, [{"type": "heartbeat", "ts": 2000, "event_id": "stale"}])
    assert any(row["reason"] == "stale_feed" for row in logs)
    assert any(row["kind"] == "cancel_request" for row in logs)
    assert not any(row["kind"] == "quote_submit" for row in logs)
    assert any(
        order.status == "cancel_requested" for order in engine.state.orders.values()
    )
    # A print during the cancel window still fills.
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "stale-trade",
                "ts": 2100,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "6",
                "taker_side": "BUY",
                "trade_id": "stale-trade",
            }
        ],
    )
    assert engine.state.markets["m1"].up == Decimal("15")


def test_reconnect_duplicate_and_old_book_do_not_rewind(tmp_path):
    engine = make_engine(tmp_path, cancel_latency_ms=500)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.48", "0.52", ts=100, ask_size="1"),
            {"type": "heartbeat", "ts": 100, "event_id": "hb"},
            {"type": "feed", "ts": 120, "event_id": "feed-down", "status": "down"},
            {"type": "feed", "ts": 200, "event_id": "feed-up", "status": "up"},
        ],
    )
    cash = engine.state.cash
    run(
        engine,
        [
            book_event("m1", "up", "0.10", "0.12", ts=50, digest="old"),
            {
                "type": "trade",
                "event_id": "dup-1",
                "ts": 210,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "6",
                "taker_side": "BUY",
                "trade_id": "same-trade",
            },
            {
                "type": "trade",
                "event_id": "dup-2",
                "ts": 211,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "6",
                "taker_side": "BUY",
                "trade_id": "same-trade",
            },
        ],
    )
    book = engine.state.markets["m1"].up_book
    assert book.best_bid() == Decimal("0.48")
    fills = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "up" and order.status == "filled"
    ]
    assert len(fills) == 1
    assert engine.state.cash > cash
    # Applying the duplicate event id is a no-op.
    again = engine.state.cash
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "dup-2",
                "ts": 300,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.99",
                "size": "100",
                "taker_side": "BUY",
                "trade_id": "other",
            }
        ],
    )
    assert engine.state.cash == again


def test_merge_cannot_spend_tokens_reserved_by_an_open_order(tmp_path):
    engine = make_engine(tmp_path)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.48", "0.52", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
        ],
    )
    logs = run(
        engine,
        [
            {
                "type": "merge",
                "event_id": "too-much",
                "ts": 1,
                "market_id": "m1",
                "amount": "20",
            }
        ],
    )
    assert any(row["reason"] == "reserved_inventory" for row in logs)
    assert engine.state.markets["m1"].up == Decimal("20")
    logs = run(
        engine,
        [
            {
                "type": "merge",
                "event_id": "ok",
                "ts": 2,
                "market_id": "m1",
                "amount": "15",
            }
        ],
    )
    assert any(row["kind"] == "merge" for row in logs)
    market = engine.state.markets["m1"]
    assert market.up == Decimal("5")
    assert market.down == Decimal("5")
    logs = run(
        engine,
        [
            {
                "type": "merge",
                "event_id": "reserved",
                "ts": 3,
                "market_id": "m1",
                "amount": "1",
            }
        ],
    )
    assert any(row["reason"] == "reserved_inventory" for row in logs)
    assert market.up == Decimal("5")


def test_fee_reserve_blocks_a_quote_that_zero_fee_would_allow(tmp_path):
    def quoted(path: Path, rate: str) -> int:
        engine = make_engine(
            path,
            risk_fee_rate=Decimal(rate),
            min_edge=Decimal("0"),
            min_order_size=Decimal("20"),
            clip_size=Decimal("20"),
            split_clip=Decimal("40"),
            min_pair_inventory=Decimal("40"),
            per_market_budget=Decimal("40"),
        )
        run(
            engine,
            [
                market_event(min_order_size="20"),
                *both_books("m1", "0.40", "0.50", ts=0, ask_size="1"),
                {"type": "heartbeat", "ts": 0, "event_id": "hb"},
            ],
        )
        return len(engine.state.orders)

    assert quoted(tmp_path / "free", "0") == 2
    assert quoted(tmp_path / "harsh", "5") == 0


def test_operational_reserve_can_refuse_a_new_market(tmp_path):
    engine = make_engine(
        tmp_path,
        operational_reserve=Decimal("31"),
        global_rounding_reserve=Decimal("0"),
    )
    logs = run(engine, [market_event()])
    assert "m1" not in engine.state.markets
    assert any(row["reason"] == "risk_floor" for row in logs)
    assert engine.state.cash == Decimal("1000")


def test_accumulated_losses_and_restart_do_not_reset_the_floor(tmp_path):
    engine = make_engine(
        tmp_path,
        min_edge=Decimal("0"),
        min_order_size=Decimal("20"),
        clip_size=Decimal("20"),
        split_clip=Decimal("40"),
        min_pair_inventory=Decimal("40"),
        per_market_budget=Decimal("40"),
        cancel_latency_ms=100,
        fill_mode="none",
    )

    def cycle(index: int, winner_side: str) -> None:
        market_id = f"m{index}"
        run(
            engine,
            [
                market_event(market_id, event_id=f"open-{market_id}", ts=index * 1000),
                *both_books(market_id, "0.02", "0.10", ts=index * 1000, ask_size="1"),
                {
                    "type": "heartbeat",
                    "ts": index * 1000,
                    "event_id": f"hb-{index}",
                    "market_id": market_id,
                },
            ],
        )
        ups = [
            order
            for order in engine.state.orders.values()
            if order.market_id == market_id and order.outcome == "up"
        ]
        downs = [
            order
            for order in engine.state.orders.values()
            if order.market_id == market_id and order.outcome == "down"
        ]
        if not ups:
            return
        run(
            engine,
            [
                {
                    "type": "adverse_fill",
                    "event_id": f"fill-{index}",
                    "ts": index * 1000 + 10,
                    "order_id": ups[0].order_id,
                    "size": str(ups[0].remaining),
                    "label": "synthetic",
                }
            ],
        )
        if downs:
            run(
                engine,
                [
                    {
                        "type": "cancel",
                        "event_id": f"cancel-{index}",
                        "ts": index * 1000 + 20,
                        "order_id": downs[0].order_id,
                    },
                    {
                        "type": "clock",
                        "event_id": f"clk-{index}",
                        "ts": index * 1000 + 200,
                    },
                ],
            )
        run(
            engine,
            [
                {
                    "type": "resolution",
                    "event_id": f"res-{index}",
                    "ts": index * 1000 + 250,
                    "market_id": market_id,
                    "source": "fixture",
                    "label": "synthetic-fixture",
                    "winner": winner_side,
                }
            ],
        )

    cycle(1, "up")
    first_floor = engine.state.loss_floor
    assert engine.state.realized_pnl < 0
    assert engine.state.markets["m1"].status == "redeemed"
    cycle(2, "up")
    assert "m2" in engine.state.markets
    assert not any(
        order.market_id == "m2"
        and order.status in {"queued", "live", "cancel_requested"}
        for order in engine.state.orders.values()
    )
    assert worst_case_equity(engine.state) >= first_floor
    loose = Config(
        initial_capital=Decimal("5000"),
        max_loss_fraction=Decimal("0.50"),
        per_market_budget=Decimal("200"),
        clip_size=Decimal("20"),
        min_order_size=Decimal("20"),
        cancel_latency_ms=0,
        fill_mode="none",
        risk_fee_rate=Decimal("0"),
    )
    resumed = Engine.load(loose, tmp_path / "state.json", tmp_path / "logs2")
    assert resumed.state.loss_floor == first_floor
    assert resumed.state.initial_capital == Decimal("1000")
    assert resumed.state.per_market_budget == Decimal("40")
    assert resumed.state.cancel_latency_ms == 100
    assert resumed.state.fill_mode == "none"
    assert resumed.state.realized_pnl == engine.state.realized_pnl
    resumed.assert_invariants()


def test_two_markets_share_one_aggregate_floor(tmp_path):
    engine = make_engine(
        tmp_path,
        min_edge=Decimal("0"),
        min_order_size=Decimal("20"),
        clip_size=Decimal("20"),
        split_clip=Decimal("40"),
        min_pair_inventory=Decimal("40"),
        per_market_budget=Decimal("40"),
        max_open_markets=2,
    )
    events = []
    for market_id in ("a", "b"):
        events.append(market_event(market_id, event_id=f"open-{market_id}", ts=0))
        events.extend(both_books(market_id, "0.10", "0.20", ts=0, ask_size="1"))
    events.append({"type": "heartbeat", "ts": 0, "event_id": "hb"})
    run(engine, events)
    working = [
        order
        for order in engine.state.orders.values()
        if order.status in {"queued", "live", "cancel_requested"}
    ]
    markets_quoting = {order.market_id for order in working}
    assert markets_quoting == {"a"}
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_oversize_split_and_adverse_fill_cannot_short_or_overspend(tmp_path):
    engine = make_engine(tmp_path, auto_split=False)
    run(
        engine,
        [
            market_event(),
            {
                "type": "split",
                "event_id": "s",
                "ts": 1,
                "market_id": "m1",
                "amount": "100000",
            },
        ],
    )
    assert engine.state.cash == Decimal("1000")
    run(
        engine,
        [
            {
                "type": "split",
                "event_id": "s2",
                "ts": 2,
                "market_id": "m1",
                "amount": "20",
            }
        ],
    )
    assert engine.state.cash == Decimal("980")
    from paper_split_maker.models import Order

    engine.state.orders["o"] = Order(
        order_id="o",
        market_id="m1",
        outcome="up",
        price=Decimal("0.40"),
        size=Decimal("5"),
        remaining=Decimal("5"),
        queue_ahead=Decimal("0"),
        status="queued",
        submit_ts=2,
    )
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "big",
                "ts": 3,
                "order_id": "o",
                "size": "6",
                "label": "synthetic",
            }
        ],
    )
    assert engine.state.markets["m1"].up == Decimal("20")
    assert engine.state.orders["o"].remaining == Decimal("5")


def test_adverse_fill_is_refused_on_a_non_synthetic_market(tmp_path):
    engine = make_engine(tmp_path, auto_split=False)
    event = market_event()
    event["synthetic"] = False
    run(
        engine,
        [
            event,
            {
                "type": "split",
                "event_id": "s",
                "ts": 1,
                "market_id": "m1",
                "amount": "10",
            },
        ],
    )
    from paper_split_maker.models import Order

    engine.state.orders["o"] = Order(
        order_id="o",
        market_id="m1",
        outcome="up",
        price=Decimal("0.40"),
        size=Decimal("5"),
        remaining=Decimal("5"),
        queue_ahead=Decimal("0"),
        status="live",
        submit_ts=1,
        live_ts=1,
    )
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "nope",
                "ts": 2,
                "order_id": "o",
                "size": "5",
                "label": "synthetic",
            }
        ],
    )
    assert engine.state.orders["o"].remaining == Decimal("5")
    assert engine.state.markets["m1"].up == Decimal("10")


def test_untrusted_and_conflicting_resolution_do_not_pay(tmp_path):
    engine = make_engine(tmp_path, auto_split=False, merge_when_idle=False)
    run(
        engine,
        [
            market_event(),
            {
                "type": "split",
                "event_id": "s",
                "ts": 1,
                "market_id": "m1",
                "amount": "10",
            },
        ],
    )
    run(
        engine,
        [
            {
                "type": "resolution",
                "event_id": "price-is-not-resolution",
                "ts": 2,
                "market_id": "m1",
                "source": "last-trade",
                "winner": "up",
            },
            {
                "type": "resolution",
                "event_id": "bad-payout",
                "ts": 3,
                "market_id": "m1",
                "source": "data-api-v2",
                "status": "resolved",
                "payouts": [1, 0],
                "market_type": "BINARY",
            },
        ],
    )
    assert engine.state.markets["m1"].status == "open"
    assert engine.state.cash == Decimal("990")
    run(
        engine,
        [
            {
                "type": "resolution",
                "event_id": "official",
                "ts": 4,
                "market_id": "m1",
                "source": "data-api-v2",
                "status": "resolved",
                "payouts": [1_000_000, 0],
                "market_type": "BINARY",
            }
        ],
    )
    assert engine.state.markets["m1"].status == "redeemed"
    assert engine.state.markets["m1"].winner == "up"
    assert engine.state.cash == Decimal("1000")
    run(
        engine,
        [
            {
                "type": "resolution",
                "event_id": "conflict",
                "ts": 5,
                "market_id": "m1",
                "source": "data-api-v2",
                "status": "resolved",
                "payouts": [0, 1_000_000],
                "market_type": "BINARY",
            }
        ],
    )
    assert engine.state.cash == Decimal("1000")
    assert engine.state.markets["m1"].winner == "up"


def test_fixture_resolution_rejected_for_non_synthetic_market(tmp_path):
    engine = make_engine(tmp_path, auto_split=False)
    event = market_event(synthetic=False)
    run(
        engine,
        [
            event,
            {
                "type": "split",
                "event_id": "s",
                "ts": 1,
                "market_id": "m1",
                "amount": "10",
            },
            {
                "type": "resolution",
                "event_id": "fix",
                "ts": 2,
                "market_id": "m1",
                "source": "fixture",
                "label": "synthetic-fixture",
                "winner": "down",
            },
        ],
    )
    assert engine.state.markets["m1"].status == "open"
    assert engine.state.cash == Decimal("990")


def test_near_expiry_and_cooldown_do_not_open_new_risk(tmp_path):
    engine = make_engine(
        tmp_path,
        stop_quoting_before_expiry_ms=5_000,
        quote_cooldown_ms=1_000,
        cancel_latency_ms=0,
    )
    run(
        engine,
        [
            market_event(end_ts=10_000),
            *both_books("m1", "0.48", "0.52", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb0"},
        ],
    )
    assert engine.state.orders
    # Inside the cooldown, a fresh book must not add another order.
    before = set(engine.state.orders)
    run(
        engine,
        [
            *both_books("m1", "0.47", "0.53", ts=100, ask_size="1"),
            {"type": "heartbeat", "ts": 100, "event_id": "hb-cool"},
        ],
    )
    assert set(engine.state.orders) == before
    # Inside the pre-expiry window, working sells are cancelled and no replacement is admitted.
    logs = run(engine, [{"type": "heartbeat", "ts": 6_000, "event_id": "hb-stop"}])
    assert any(row["reason"] == "near_expiry" for row in logs)
    assert not any(row["kind"] == "quote_submit" and row["ts"] == 6000 for row in logs)
    run(engine, [{"type": "clock", "ts": 6_000, "event_id": "confirm-stop"}])
    assert all(
        order.status in {"cancelled", "filled"}
        for order in engine.state.orders.values()
    )
    assert engine.state.markets["m1"].winner is None
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_no_fill_mode_ignores_prints(tmp_path):
    engine = make_engine(tmp_path, fill_mode="none")
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.48", "0.52", ts=0, ask_size="1"),
            {"type": "heartbeat", "ts": 0, "event_id": "hb"},
            {"type": "clock", "ts": 5, "event_id": "live"},
            {
                "type": "trade",
                "event_id": "t",
                "ts": 6,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.52",
                "size": "100",
                "taker_side": "BUY",
                "trade_id": "t1",
            },
        ],
    )
    assert engine.state.markets["m1"].up == Decimal("20")


def test_old_state_keeps_its_floor_and_ignores_removed_fields(tmp_path, capsys):
    """A ledger written before the cleanup still loads, and the old floor stays."""
    engine = make_engine(
        tmp_path,
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("30"),
    )
    run(engine, [market_event()])
    path = tmp_path / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["max_pair_loss"] = "0.01"
    market = next(iter(raw["markets"].values()))
    market["last_mid"] = "0.50"
    market["last_mid_ts"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    newer = Config(
        initial_capital=Decimal("59"),
        max_loss_fraction=Decimal("0.20"),
        per_market_budget=Decimal("25"),
    )
    resumed = Engine.load(newer, path, tmp_path / "logs2")
    assert resumed.state.loss_floor == engine.state.loss_floor
    assert resumed.state.initial_capital == Decimal("30")
    assert resumed.state.markets["m1"].up == engine.state.markets["m1"].up
    printed = capsys.readouterr().out
    assert "locked bankroll" in printed
    assert "29.10" in printed


def test_config_rejects_forbidden_and_loose_fraction():
    with pytest.raises(ConfigError):
        config_from_mapping({"private_key": "nope"})
    with pytest.raises(ConfigError):
        config_from_mapping({"initial_capital": "1000", "max_loss_fraction": "1"})
    with pytest.raises(ConfigError):
        config_from_mapping({"live_trading": True})


def test_randomized_actions_keep_the_floor(tmp_path):
    rng = random.Random(3318)
    for trial in range(8):
        engine = make_engine(
            tmp_path / f"trial-{trial}",
            min_order_size=Decimal("1"),
            clip_size=Decimal("5"),
            order_size_quantum=Decimal("1"),
            split_clip=Decimal("10"),
            min_pair_inventory=Decimal("5"),
            per_market_budget=Decimal("30"),
            min_edge=Decimal("0"),
            cancel_latency_ms=50,
            ack_latency_ms=10,
            fill_mode="queue",
            merge_when_idle=True,
        )
        end_ts = 10**9
        run(
            engine,
            [
                market_event(
                    "m", end_ts=end_ts, min_order_size="1", event_id=f"open-{trial}"
                )
            ],
        )
        ts = 0
        seq = 0
        for _step in range(18):
            ts += rng.randint(1, 80)
            seq += 1
            roll = rng.randrange(7)
            if roll == 0:
                bid = Decimal(rng.choice(["0.20", "0.40", "0.60", "0.80"]))
                ask = bid + Decimal("0.02")
                run(
                    engine,
                    both_books(
                        "m",
                        format(bid, "f"),
                        format(ask, "f"),
                        ts=ts,
                        ask_size=str(rng.choice(["0", "5", "20"])),
                    ),
                )
            elif roll == 1:
                run(
                    engine,
                    [{"type": "heartbeat", "ts": ts, "event_id": f"hb-{trial}-{seq}"}],
                )
            elif roll == 2:
                run(
                    engine,
                    [
                        {
                            "type": "trade",
                            "event_id": f"tr-{trial}-{seq}",
                            "ts": ts,
                            "market_id": "m",
                            "outcome": rng.choice(["up", "down"]),
                            "price": rng.choice(["0.22", "0.42", "0.62", "0.82"]),
                            "size": str(rng.choice(["1", "5", "30"])),
                            "taker_side": rng.choice(["BUY", "SELL"]),
                            "trade_id": f"trade-{trial}-{seq}",
                        }
                    ],
                )
            elif roll == 3:
                working = [
                    order
                    for order in engine.state.orders.values()
                    if order.status in {"queued", "live", "cancel_requested"}
                ]
                if working:
                    order = rng.choice(working)
                    run(
                        engine,
                        [
                            {
                                "type": "adverse_fill",
                                "event_id": f"adv-{trial}-{seq}",
                                "ts": ts,
                                "order_id": order.order_id,
                                "size": str(
                                    order.remaining
                                    + rng.choice([Decimal("0"), Decimal("1")])
                                ),
                                "label": "synthetic",
                            }
                        ],
                    )
            elif roll == 4:
                run(
                    engine,
                    [
                        {
                            "type": "merge",
                            "event_id": f"mg-{trial}-{seq}",
                            "ts": ts,
                            "market_id": "m",
                            "amount": str(rng.choice(["1", "5", "100"])),
                        }
                    ],
                )
            elif roll == 5:
                run(
                    engine,
                    [
                        {
                            "type": "feed",
                            "event_id": f"fd-{trial}-{seq}",
                            "ts": ts,
                            "status": rng.choice(["up", "down"]),
                        }
                    ],
                )
            else:
                run(
                    engine,
                    [
                        {
                            "type": "cancel",
                            "event_id": f"cx-{trial}-{seq}",
                            "ts": ts,
                            "market_id": "m",
                        }
                    ],
                )
            engine.assert_invariants()
            assert engine.state.cash >= 0
            assert worst_case_equity(engine.state) >= engine.state.loss_floor
        revived = Engine(State.from_dict(engine.state.to_dict()), engine.config)
        revived.assert_invariants()
        assert revived.state.loss_floor == engine.state.loss_floor
        assert revived.state.realized_pnl == engine.state.realized_pnl
        assert revived.state.cash == engine.state.cash
