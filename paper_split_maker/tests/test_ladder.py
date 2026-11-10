"""Ladder quotes: several post-only rungs, skew, and the last minute."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paper_split_maker.config import Config
from paper_split_maker.ladder_replay import replay_session
from tests.helpers import book_event, make_engine, market_event, run

ROOT = Path(__file__).resolve().parents[1]


def _engine(tmp_path, **extra):
    values = dict(
        strategy="ladder",
        initial_capital=Decimal("59"),
        max_loss_fraction=Decimal("0.20"),
        per_market_budget=Decimal("25"),
        split_clip=Decimal("25"),
        pair_min_edge=Decimal("0"),
        max_unpaired_fraction=Decimal("0.60"),
        ladder_levels=3,
        ladder_spacing=Decimal("0.01"),
        skew_mid_gap=Decimal("0.04"),
        late_window_seconds=60,
        winner_min_price=Decimal("0.90"),
        min_order_rest_seconds=0,
        requote_min_interval_ms=0,
        stop_quoting_before_expiry_ms=5_000,
        min_edge=Decimal("0"),
        risk_fee_rate=Decimal("0.07"),
        operational_reserve=Decimal("0.05"),
        global_rounding_reserve=Decimal("0.01"),
        merge_when_idle=False,
        auto_split=False,
        session_max_loss=Decimal("6"),
        quote_cooldown_ms=0,
    )
    values.update(extra)
    return make_engine(tmp_path, **values)


def _arm(engine, end_ts=300_000):
    run(
        engine,
        [
            market_event(end_ts=end_ts),
            {
                "type": "split",
                "event_id": "split-25",
                "ts": 1,
                "market_id": "m1",
                "amount": "25",
            },
        ],
    )


def _quote(engine, books, ts):
    events = []
    for outcome, bid, ask in books:
        events.append(
            book_event(
                "m1",
                outcome,
                bid,
                ask,
                ts=ts,
                digest=f"{outcome}-{bid}-{ask}-{ts}",
            )
        )
    events.append(
        {"type": "heartbeat", "event_id": f"h-{ts}", "ts": ts, "market_id": "m1"}
    )
    run(engine, events)


def _prices(engine, outcome):
    return sorted(
        order.price
        for order in engine.state.orders.values()
        if order.outcome == outcome and order.status in {"queued", "live"}
    )


def test_neutral_book_posts_three_rungs_each_side(tmp_path):
    engine = _engine(tmp_path)
    _arm(engine)
    _quote(
        engine,
        [("up", "0.49", "0.51"), ("down", "0.49", "0.51")],
        10_000,
    )
    assert _prices(engine, "up") == [
        Decimal("0.50"),
        Decimal("0.51"),
        Decimal("0.52"),
    ]
    assert _prices(engine, "down") == _prices(engine, "up")
    sizes = [
        order.size
        for order in engine.state.orders.values()
        if order.status in {"queued", "live"}
    ]
    assert sizes
    assert set(sizes) == {Decimal("5")}


def test_cheap_side_joins_the_touch_and_rich_side_steps_back(tmp_path):
    engine = _engine(tmp_path)
    _arm(engine)
    _quote(
        engine,
        [("up", "0.70", "0.72"), ("down", "0.28", "0.30")],
        20_000,
    )
    assert _prices(engine, "down")[0] == Decimal("0.29")
    assert len(_prices(engine, "down")) == 3
    assert _prices(engine, "up")[0] == Decimal("0.72")
    assert len(_prices(engine, "up")) == 2


def test_last_minute_sells_the_winner_high_and_dumps_the_loser(tmp_path):
    engine = _engine(tmp_path)
    _arm(engine)
    _quote(
        engine,
        [("up", "0.94", "0.96"), ("down", "0.04", "0.06")],
        250_000,
    )
    assert _prices(engine, "up") == [Decimal("0.95")]
    assert _prices(engine, "down")[0] == Decimal("0.05")
    assert all(price <= Decimal("0.06") for price in _prices(engine, "down"))


def test_tight_budget_posts_nothing_at_mid_and_ladder_does(tmp_path):
    tight = make_engine(
        tmp_path / "tight",
        initial_capital=Decimal("59"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("25"),
        split_clip=Decimal("25"),
        pair_min_edge=Decimal("0.01"),
        min_edge=Decimal("0"),
        risk_fee_rate=Decimal("0"),
        merge_when_idle=False,
        auto_split=False,
    )
    _arm(tight)
    _quote(
        tight,
        [("up", "0.49", "0.51"), ("down", "0.49", "0.51")],
        10_000,
    )
    assert _prices(tight, "up") == []
    assert _prices(tight, "down") == []
    wide = _engine(tmp_path / "wide")
    _arm(wide)
    _quote(
        wide,
        [("up", "0.49", "0.51"), ("down", "0.49", "0.51")],
        10_000,
    )
    assert len(_prices(wide, "up")) == 3


def test_bid_at_breakeven_still_takes_the_other_leg(tmp_path):
    engine = _engine(tmp_path)
    _arm(engine)
    _quote(
        engine,
        [("up", "0.49", "0.51"), ("down", "0.49", "0.51")],
        10_000,
    )
    run(
        engine,
        [
            {"type": "heartbeat", "event_id": "live", "ts": 10_001, "market_id": "m1"}
        ],
    )
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "fill-down",
                "ts": 10_002,
                "trade_ts": 10_002,
                "market_id": "m1",
                "outcome": "down",
                "price": "0.50",
                "size": "5",
                "taker_side": "BUY",
                "trade_id": "fill-1",
            }
        ],
    )
    assert engine.state.markets["m1"].sold_down_shares == Decimal("5")
    _quote(engine, [("up", "0.55", "0.57"), ("down", "0.43", "0.45")], 11_000)
    taken = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "up" and order.order_id in engine.marketable_ids
    ]
    assert taken
    assert taken[0].price >= Decimal("0.50")


def test_cheap_leg_keeps_quoting_when_the_bid_is_below_breakeven(tmp_path):
    engine = _engine(tmp_path)
    _arm(engine)
    _quote(
        engine,
        [("up", "0.49", "0.51"), ("down", "0.49", "0.51")],
        10_000,
    )
    run(
        engine,
        [
            {"type": "heartbeat", "event_id": "live", "ts": 10_001, "market_id": "m1"},
            {
                "type": "trade",
                "event_id": "fill-down",
                "ts": 10_002,
                "trade_ts": 10_002,
                "market_id": "m1",
                "outcome": "down",
                "price": "0.50",
                "size": "5",
                "taker_side": "BUY",
                "trade_id": "fill-1",
            },
        ],
    )
    _quote(engine, [("up", "0.20", "0.22"), ("down", "0.78", "0.80")], 12_000)
    assert engine.marketable_ids == set() or all(
        order.outcome != "up" or order.order_id not in engine.marketable_ids
        for order in engine.state.orders.values()
    )
    assert _prices(engine, "up")
    assert _prices(engine, "up")[0] < Decimal("0.50")


def test_replay_sells_the_loser_and_posts_more_than_the_tight_book(tmp_path):
    prints = [
        {"sec": 30, "outcome": "down", "price": "0.48", "size": "10"},
        {"sec": 40, "outcome": "down", "price": "0.48", "size": "10"},
        {"sec": 80, "outcome": "down", "price": "0.40", "size": "10"},
        {"sec": 81, "outcome": "down", "price": "0.40", "size": "10"},
        {"sec": 140, "outcome": "down", "price": "0.25", "size": "10"},
        {"sec": 141, "outcome": "down", "price": "0.25", "size": "10"},
        {"sec": 200, "outcome": "up", "price": "0.80", "size": "5"},
        {"sec": 250, "outcome": "down", "price": "0.08", "size": "10"},
        {"sec": 251, "outcome": "down", "price": "0.08", "size": "10"},
        {"sec": 260, "outcome": "up", "price": "0.93", "size": "5"},
    ]
    ladder = _engine(tmp_path / "ladder").config
    result = replay_session(ladder, tmp_path / "out-ladder", prints, "up")
    assert result["sold_down"] > result["sold_up"]
    assert result["sold_down"] >= Decimal("5")
    tight = Config(
        initial_capital=Decimal("59"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("25"),
        split_clip=Decimal("25"),
        pair_min_edge=Decimal("0.01"),
        strategy="single",
        min_edge=Decimal("0"),
        risk_fee_rate=Decimal("0"),
        merge_when_idle=False,
        auto_split=False,
        min_order_rest_seconds=0,
    )
    single = replay_session(tight, tmp_path / "out-single", prints, "up")
    assert result["sold_down"] + result["sold_up"] > single["sold_down"] + single[
        "sold_up"
    ]


def test_queue_priority_keeps_a_rung_until_the_price_moves_a_tick(tmp_path):
    engine = _engine(tmp_path, queue_priority=True, min_order_rest_seconds=0)
    _arm(engine)
    _quote(engine, [("up", "0.49", "0.51"), ("down", "0.49", "0.51")], 10_000)
    before = _prices(engine, "up")
    assert before
    # Half a tick of noise is not a reason to lose the queue.
    _quote(engine, [("up", "0.49", "0.51"), ("down", "0.49", "0.51")], 11_000)
    assert _prices(engine, "up") == before
    resting = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "up" and order.status in {"queued", "live"}
    ]
    assert resting
    assert all(order.status != "cancel_requested" for order in resting)


def test_a_bid_that_reaches_the_rung_is_pulled(tmp_path):
    engine = _engine(tmp_path, queue_priority=True, min_order_rest_seconds=30)
    _arm(engine)
    _quote(engine, [("up", "0.49", "0.51"), ("down", "0.49", "0.51")], 10_000)
    order = next(
        item
        for item in engine.state.orders.values()
        if item.outcome == "up" and item.status in {"queued", "live"}
    )
    _quote(
        engine,
        [("up", str(order.price), "0.99"), ("down", "0.01", "0.02")],
        12_000,
    )
    assert order.status in {"cancel_requested", "cancelled"}


def test_matched_sell_stays_at_or_above_one_minus_the_other_fill(tmp_path):
    engine = _engine(tmp_path, queue_priority=True, pair_min_edge=Decimal("0"))
    _arm(engine)
    _quote(engine, [("up", "0.39", "0.41"), ("down", "0.59", "0.61")], 10_000)
    down = next(
        item
        for item in engine.state.orders.values()
        if item.outcome == "down" and item.status in {"queued", "live"}
    )
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "fill-down",
                "ts": 11_000,
                "trade_ts": 11_000,
                "market_id": "m1",
                "outcome": "down",
                "price": format(down.price, "f"),
                "size": "5",
                "taker_side": "BUY",
                "trade_id": "fill-down",
            }
        ],
    )
    _quote(engine, [("up", "0.39", "0.41"), ("down", "0.59", "0.61")], 12_000)
    up_prices = _prices(engine, "up")
    if up_prices:
        assert min(up_prices) + down.price >= Decimal("1")


def test_preopen_lead_does_not_end_the_session_early(tmp_path):
    engine = _engine(
        tmp_path,
        split_lead_seconds=300,
        stop_quoting_before_expiry_ms=5_000,
    )
    _arm(engine)
    _quote(engine, [("up", "0.49", "0.51"), ("down", "0.49", "0.51")], 10_000)
    assert _prices(engine, "up")
    assert engine._preclose_ms(engine.state.markets["m1"]) == 300_000 - 5_000
