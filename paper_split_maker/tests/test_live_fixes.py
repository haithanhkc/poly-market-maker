"""Regression tests for live split confirmation, live fills, and Windows printing."""

from __future__ import annotations

import io
import sys
from decimal import Decimal
from types import SimpleNamespace

from paper_split_maker import cli, live_runner
from paper_split_maker.chain_send import erc1155_balance, split_tokens_present
from tests.helpers import both_books, make_engine, market_event, run

FUNDER = "0xC6C1B52664581bc51f18eC0DB9FE8ddF14396a45"


def _fake_rpc(balances):
    def call(to, data):
        token = int(data[-64:], 16)
        assert data.startswith("0x00fdd58e")
        assert FUNDER.lower()[2:] in data
        return hex(balances.get(token, 0))

    return call


def test_erc1155_balance_and_split_tokens_present():
    rpc = _fake_rpc({11: 5_000_000, 22: 5_000_000})
    assert erc1155_balance(FUNDER, "11", rpc) == 5_000_000
    assert split_tokens_present(FUNDER, "11", "22", Decimal("5"), rpc)
    assert not split_tokens_present(FUNDER, "11", "33", Decimal("5"), rpc)
    assert not split_tokens_present(FUNDER, "11", "22", Decimal("6"), rpc)


def _pending_engine(tmp_path):
    engine = make_engine(
        tmp_path,
        auto_split=False,
        split_clip=Decimal("5"),
        min_pair_inventory=Decimal("5"),
        clip_size=Decimal("5"),
    )
    run(
        engine,
        [
            market_event("btc-updown-5m-0", up_token_id="11", down_token_id="22"),
            {
                "type": "split_pending",
                "event_id": "p",
                "ts": 1,
                "market_id": "btc-updown-5m-0",
                "amount": "5",
            },
        ],
    )
    engine.split_tx = {"btc-updown-5m-0": "0xabc"}
    engine.split_settled = {"btc-updown-5m-0"}
    return engine


def test_settled_split_waits_for_onchain_tokens(tmp_path, monkeypatch):
    import paper_split_maker.chain_send as cs

    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda s: s)
    engine = _pending_engine(tmp_path)
    monkeypatch.setattr(cs, "split_tokens_present", lambda *a, **k: False)
    notes = []
    live_runner._confirm_splits(engine, creds, 2, notes)
    market = engine.state.markets["btc-updown-5m-0"]
    assert market.up == 0 and market.pending_split == Decimal("5")
    assert "btc-updown-5m-0" in engine.split_tx

    monkeypatch.setattr(cs, "split_tokens_present", lambda *a, **k: True)
    live_runner._confirm_splits(engine, creds, 3, notes)
    assert market.up == Decimal("5") and market.down == Decimal("5")
    assert not engine.split_tx


def test_live_mode_ignores_public_trades(tmp_path):
    engine = make_engine(tmp_path, clip_size=Decimal("5"))
    engine.mode = "live"
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    order = next(o for o in engine.state.orders.values() if o.status == "live")
    cash = engine.state.cash
    logs = run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "t1",
                "ts": 10,
                "market_id": "m1",
                "trade_id": "x1",
                "outcome": order.outcome,
                "taker_side": "BUY",
                "price": "0.99",
                "size": "100",
            }
        ],
    )
    assert engine.state.cash == cash
    assert not [r for r in logs if r["kind"] == "fill"]
    assert any(r.get("reason") == "live_mode_no_model_fill" for r in logs)
    logs = run(
        engine,
        [
            {
                "type": "user_fill",
                "event_id": "u1",
                "ts": 11,
                "order_id": order.order_id,
                "trade_id": "ex1",
                "size": "1",
            }
        ],
    )
    fills = [r for r in logs if r["kind"] == "fill"]
    assert fills and fills[0]["reason"] == "exchange_fill"


def test_rejected_post_is_not_resting(tmp_path):
    engine = make_engine(tmp_path, clip_size=Decimal("5"))
    engine.mode = "live"
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 1},
        ],
    )
    queued = [o for o in engine.state.orders.values() if o.status == "queued"]
    assert queued

    def post(client, orders):
        return [
            {"success": False, "errorMsg": "not enough balance / allowance"}
            for _ in orders
        ]

    creds = SimpleNamespace(redact=lambda s: s)
    notes = []
    live_runner._send_new_quotes(engine, None, post, creds, notes)
    for order in queued:
        assert order.status == "rejected" and order.exchange_id == ""
    assert engine.state.markets["m1"].pause_until_ms > engine.state.now_ms
    assert any("order rejected" in n for n in notes)


def test_summary_print_survives_gbk_console(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    cli._print_summary({"mode": "live", "disclaimer_vi": "Lợi nhuận không đảm bảo ✓"})
    stream.flush()
    assert "live".encode() in raw.getvalue()


def test_sell_never_crosses_best_bid(tmp_path):
    from paper_split_maker.quotes import Proposal
    engine = make_engine(tmp_path)
    run(engine, [market_event(), *both_books("m1", "0.40", "0.50", 1)])
    market = engine.state.markets["m1"]
    prop = Proposal(outcome="up", price=Decimal("0.40"), size=Decimal("5"),
                    fair=Decimal("0.40"), required_edge=Decimal("0"), repair=False)
    engine._place_one(market, prop, Decimal("0.40"), False)
    for order in engine.state.orders.values():
        if order.outcome == "up" and order.status in {"queued", "live"}:
            assert order.price > Decimal("0.40")


def test_run5e_cancel_pending_orders_stay_exposure(tmp_path):
    """run5e: Down sells at 0.87-0.96 re-posted while cancels were pending."""
    from paper_split_maker.quotes import Proposal
    from paper_split_maker.risk import worst_case_equity
    engine = make_engine(tmp_path, initial_capital=Decimal("30"), per_market_budget=Decimal("30"), clip_size=Decimal("30"))
    engine.config.live = True
    run(engine, [market_event(), *both_books("m1", "0.04", "0.97", 1)])
    market = engine.state.markets["m1"]
    engine._split(market, Decimal("30"), "test")
    for k, price in enumerate(["0.87", "0.92", "0.96", "0.94", "0.95", "0.96"]):
        engine.state.now_ms = 1000 + k * 20000
        prop = Proposal(outcome="down", price=Decimal(price), size=Decimal("30"),
                        fair=Decimal(price), required_edge=Decimal("0"), repair=False)
        engine._place_one(market, prop, Decimal(price), False)
        engine._promote()
        for order in list(engine.state.orders.values()):
            order.exchange_id = order.exchange_id or "x-" + order.order_id
            engine._request_cancel(market, order, "requote")
        engine._promote()
    total = sum(o.remaining for o in engine.state.orders.values()
                if o.outcome == "down" and o.status != "rejected")
    # Worst case: every not-confirmed order fills and Down wins.
    loss = Decimal("30") - (engine.state.cash + sum(
        o.remaining * o.price for o in engine.state.orders.values()
        if o.outcome == "down" and o.status != "rejected") + (market.down - total))
    assert total <= market.down
    assert loss <= Decimal("0.90")
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_three_consecutive_markets_are_accepted(tmp_path):
    engine = make_engine(tmp_path, initial_capital=Decimal("30"),
                         per_market_budget=Decimal("30"), clip_size=Decimal("30"))
    for k, mid in enumerate(["a1", "a2", "a3"]):
        run(engine, [market_event(mid, end_ts=(k + 1) * 300_000,
                                  event_id=f"open-{mid}", ts=k * 300_000)])
        assert mid in engine.state.markets
        market = engine.state.markets[mid]
        engine.state.now_ms = k * 300_000 + 1
        assert engine._split(market, Decimal("25"), "test")
        # session ends with a small unpaired leftover, merged otherwise
        market.down -= Decimal("1")
        engine.state.cash += Decimal("0.97")
        engine._merge(market, market.down, "test")
        market.status = "expired_unresolved"
