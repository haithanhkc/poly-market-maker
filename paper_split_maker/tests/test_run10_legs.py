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
        pre_split_lead_ms=45_000,
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
    assert market.up == Decimal("0")
    assert market.down == Decimal("0")


def test_buy_pair_enters_only_under_the_edge_and_unwinds_one_leg(tmp_path):
    engine = _capital30(
        tmp_path,
        entry_mode="buy_pair_arb",
        pair_min_edge=Decimal("0.02"),
        risk_fee_rate=Decimal("0"),
        clip_size=Decimal("5"),
        min_order_size=Decimal("5"),
    )
    run(
        engine,
        [
            market_event(end_ts=300_000),
            book_event("m1", "up", "0.39", "0.40", ts=1, ask_size="10"),
            book_event("m1", "down", "0.39", "0.40", ts=1, ask_size="10"),
            {"type": "heartbeat", "event_id": "buy", "ts": 2},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.gross_split == Decimal("0")
    assert market.up == Decimal("0") and market.down == Decimal("0")
    assert engine.state.cash > Decimal("30")
    locked = engine.state.cash
    run(
        engine,
        [
            book_event("m1", "up", "0.49", "0.50", ts=3, ask_size="10"),
            book_event("m1", "down", "0.49", "0.50", ts=3, ask_size="10"),
            {"type": "heartbeat", "event_id": "skip", "ts": 4},
        ],
    )
    assert engine.state.cash == locked
    engine.buy_fail_outcome = "down"
    run(
        engine,
        [
            book_event("m1", "up", "0.39", "0.40", ts=5, ask_size="10"),
            book_event("m1", "down", "0.39", "0.40", ts=5, ask_size="10"),
            {"type": "heartbeat", "event_id": "one-leg", "ts": 6},
        ],
    )
    assert market.up == Decimal("0") and market.down == Decimal("0")
    assert engine.state.cash < locked


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
