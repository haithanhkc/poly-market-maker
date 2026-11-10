"""One filled leg must not freeze the tick, and the other leg is sold."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from paper_split_maker.live_runner import _shutdown_live
from paper_split_maker.logs import build_summary
from paper_split_maker.models import Order
from paper_split_maker.risk import worst_case_equity
from tests.helpers import book_event, make_engine, market_event, run


def _capital30(tmp_path, **extra):
    values = dict(
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("30"),
        clip_size=Decimal("30"),
        split_clip=Decimal("30"),
        min_order_size=Decimal("5"),
        pair_min_edge=Decimal("0.01"),
        max_unpaired_fraction=Decimal("0.5"),
        risk_fee_rate=Decimal("0.07"),
        maker_fee_rate=Decimal("0"),
        operational_reserve=Decimal("0.05"),
        global_rounding_reserve=Decimal("0.01"),
        auto_split=False,
        merge_when_idle=False,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        fill_mode="none",
        leg_unwind_seconds=5,
        stale_after_ms=1_000_000,
        stop_quoting_before_expiry_ms=20_000,
        split_lead_seconds=45,
    )
    values.update(extra)
    return make_engine(tmp_path, **values)


def _rest(engine, order_id, outcome, price, size):
    engine.state.orders[order_id] = Order(
        order_id=order_id,
        market_id="m1",
        outcome=outcome,
        price=Decimal(price),
        size=Decimal(size),
        remaining=Decimal(size),
        queue_ahead=Decimal("0"),
        status="live",
        submit_ts=1,
        live_ts=1,
    )


def test_one_filled_leg_does_not_freeze_and_the_other_is_sold(tmp_path):
    engine = _capital30(tmp_path)
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            book_event("m1", "down", "0.55", "0.60", ts=1),
            book_event("m1", "up", "0.40", "0.47", ts=1),
        ],
    )
    _rest(engine, "down-15", "down", "0.60", "15")
    _rest(engine, "up-15", "up", "0.47", "15")
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "fill-down",
                "ts": 1000,
                "label": "synthetic",
                "order_id": "down-15",
                "size": "15",
            }
        ],
    )
    market = engine.state.markets["m1"]
    assert market.down == Decimal("15")
    assert market.up == Decimal("30")
    worst = worst_case_equity(engine.state)
    assert worst == Decimal("23.94")
    assert worst < engine.state.loss_floor
    # The old tick raised here and never sold the leftover Up.
    run(engine, [{"type": "heartbeat", "event_id": "unwind", "ts": 7000}])
    assert market.up == Decimal("0")
    assert market.down == Decimal("0")
    run(engine, [{"type": "heartbeat", "event_id": "still-ticking", "ts": 8000}])
    assert engine.state.cash > Decimal("9")


def test_partial_fill_unwinds_only_the_naked_shares(tmp_path):
    engine = _capital30(tmp_path)
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            book_event("m1", "down", "0.55", "0.60", ts=1),
            book_event("m1", "up", "0.40", "0.47", ts=1),
        ],
    )
    _rest(engine, "down-15", "down", "0.60", "15")
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "fill-part",
                "ts": 1000,
                "label": "synthetic",
                "order_id": "down-15",
                "size": "6",
            },
            {"type": "heartbeat", "event_id": "unwind", "ts": 7000},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.sold_up_shares == Decimal("6")
    # The unsold Down quote stays. What remains is a matched pair, not a naked leg.
    assert market.up == market.down
    assert market.sold_down_shares - market.sold_up_shares == Decimal("0")


def test_flat_session_does_not_block_the_next(tmp_path):
    engine = _capital30(tmp_path, max_open_markets=1)
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            {"type": "merge", "event_id": "m", "ts": 2, "market_id": "m1", "amount": "30"},
        ],
    )
    # A chain merge can still be queued after the ledger is flat.
    engine.chain_outbox.append({"action": "merge", "market_id": "m1"})
    run(engine, [market_event(market_id="m2", end_ts=600_000, ts=3)])
    assert "m2" in engine.state.markets
    assert engine._split(engine.state.markets["m2"], Decimal("5"), "next") is True


def test_realized_loss_stops_new_entries(tmp_path):
    engine = _capital30(tmp_path, auto_split=False)
    run(engine, [market_event(end_ts=300_000)])
    engine.state.realized_pnl = Decimal("-0.90")
    run(engine, [{"type": "heartbeat", "event_id": "hb", "ts": 1}])
    assert engine.state.killed
    market = engine.state.markets["m1"]
    assert engine._split(market, Decimal("5"), "test") is False


def test_live_summary_is_not_marked_paper_only(tmp_path):
    engine = _capital30(tmp_path)
    summary = build_summary(engine.state, mode="live", resumed=False)
    assert summary["paper_only"] is False
    assert summary["live_orders"] is True
    paper = build_summary(engine.state, mode="paper", resumed=False)
    assert paper["paper_only"] is True


def test_fill_at_083_takes_the_bid_when_it_clears_breakeven(tmp_path):
    engine = _capital30(
        tmp_path,
        clip_size=Decimal("5"),
        split_clip=Decimal("5"),
        risk_fee_rate=Decimal("0"),
        published_taker_fee_rate=Decimal("0"),
    )
    _split_five(engine, down_bid="0.80", down_ask="0.83", up_bid="0.23", up_ask="0.25")
    _fill_down(engine, "5", "0.83")
    run(engine, [{"type": "heartbeat", "event_id": "take", "ts": 1000}])
    market = engine.state.markets["m1"]
    assert market.sold_up_shares == Decimal("5")
    assert market.sold_up_proceeds == Decimal("1.15")
    assert market.up == Decimal("0")
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "taker_breakeven" in log
    assert "floor=0.17" in log


def test_fill_at_083_rests_at_breakeven_when_the_bid_is_short(tmp_path):
    engine = _capital30(
        tmp_path,
        clip_size=Decimal("5"),
        split_clip=Decimal("5"),
        risk_fee_rate=Decimal("0"),
    )
    _split_five(engine, down_bid="0.80", down_ask="0.83", up_bid="0.15", up_ask="0.16")
    _fill_down(engine, "5", "0.83")
    run(engine, [{"type": "heartbeat", "event_id": "rest", "ts": 1000}])
    market = engine.state.markets["m1"]
    assert market.sold_up_shares == Decimal("0")
    resting = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "up" and order.status in {"queued", "live"}
    ]
    assert resting
    assert all(order.price >= Decimal("0.17") for order in resting)
    assert all(order.order_id not in engine.marketable_ids for order in resting)


def test_book_move_from_015_to_023_takes_the_leg(tmp_path):
    engine = _capital30(
        tmp_path,
        clip_size=Decimal("5"),
        split_clip=Decimal("5"),
        risk_fee_rate=Decimal("0"),
    )
    _split_five(engine, down_bid="0.80", down_ask="0.83", up_bid="0.15", up_ask="0.16")
    _fill_down(engine, "5", "0.83")
    run(
        engine,
        [
            {"type": "heartbeat", "event_id": "rest", "ts": 1000},
            book_event("m1", "up", "0.23", "0.25", ts=2000),
        ],
    )
    market = engine.state.markets["m1"]
    assert market.sold_up_shares == Decimal("5")
    assert market.sold_up_proceeds == Decimal("1.15")


def test_partial_fill_sells_only_the_matched_size(tmp_path):
    engine = _capital30(
        tmp_path,
        clip_size=Decimal("10"),
        split_clip=Decimal("10"),
        min_order_size=Decimal("5"),
        risk_fee_rate=Decimal("0"),
    )
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "10"},
            book_event("m1", "down", "0.80", "0.83", ts=1),
            book_event("m1", "up", "0.23", "0.25", ts=1),
        ],
    )
    _rest(engine, "down-10", "down", "0.83", "10")
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "part",
                "ts": 1000,
                "label": "synthetic",
                "order_id": "down-10",
                "size": "5",
            },
            {"type": "heartbeat", "event_id": "take", "ts": 2000},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.sold_up_shares == Decimal("5")
    assert market.sold_down_shares == Decimal("5")


def test_crosses_book_rejection_retries_as_taker(tmp_path, monkeypatch):
    engine = _capital30(
        tmp_path,
        clip_size=Decimal("5"),
        split_clip=Decimal("5"),
        risk_fee_rate=Decimal("0"),
    )
    engine.config.live = True
    _split_five(engine, down_bid="0.80", down_ask="0.83", up_bid="0.15", up_ask="0.16")
    _fill_down(engine, "5", "0.83")
    run(engine, [{"type": "heartbeat", "event_id": "rest", "ts": 1000}])
    resting = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "up" and order.status == "queued"
    ]
    assert resting and resting[0].price >= Decimal("0.17")
    # The bid steps through the post-only price before the exchange accepts it.
    engine.state.markets["m1"].book("up").bids = [(Decimal("0.23"), Decimal("10"))]
    engine.state.markets["m1"].book("up").asks = [(Decimal("0.25"), Decimal("10"))]
    seen = {"taker": []}

    def taker(_client, orders):
        seen["taker"].extend(orders)
        return [{"orderID": "taker-1", "success": True}]

    def maker(_client, orders):
        return [
            {
                "success": False,
                "errorMsg": "invalid post-only order: order crosses book",
                "status": 400,
            }
            for _ in orders
        ]

    monkeypatch.setattr("paper_split_maker.live_clob.post_taker_sells", taker)
    from paper_split_maker.live_runner import _send_new_quotes

    notes: list[str] = []
    creds = SimpleNamespace(redact=lambda text: text)
    _send_new_quotes(engine, object(), maker, creds, notes)
    assert seen["taker"]
    assert Decimal(seen["taker"][0]["price"]) == Decimal("0.23")
    assert Decimal(seen["taker"][0]["size"]) == Decimal("5")
    assert any("order rejected" in note for note in notes)
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "cross_retry_taker" in log


def _split_five(engine, *, down_bid, down_ask, up_bid, up_ask):
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "5"},
            book_event("m1", "down", down_bid, down_ask, ts=1),
            book_event("m1", "up", up_bid, up_ask, ts=1),
        ],
    )


def _fill_down(engine, size, price):
    _rest(engine, "down-fill", "down", price, size)
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": f"fill-{size}",
                "ts": 500,
                "label": "synthetic",
                "order_id": "down-fill",
                "size": size,
            }
        ],
    )


def test_shutdown_logs_cancel_all(tmp_path):
    engine = _capital30(tmp_path)
    notes: list[str] = []

    class Client:
        def __init__(self):
            self.called = False

        def cancel_all(self):
            self.called = True

    client = Client()
    creds = SimpleNamespace(redact=lambda text: text)
    _shutdown_live(engine, client, creds, notes, lambda holder: holder.cancel_all())
    assert client.called
    assert "cancel-all sent" in notes
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "cancel_all" in log
