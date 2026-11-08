"""Pair floor, stale feed, background redeem, and session counting. No network."""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

from paper_split_maker.live_runner import (
    ChainWorker,
    _collateral_shares,
    _consider_pre_split,
    _flush_chain,
    _lead_ms,
    _note_session_done,
    _runtime_seconds,
    redeem_pending_once,
    run_confirmed_live,
)
from paper_split_maker.normalize import resolution_settled
from paper_split_maker.quotes import pair_edge_floor, propose
from tests.helpers import book_event, make_engine, market_event, run
from tests.test_rollover import FUNDER, OPEN0, _Reader, _rollover_engine


def _sell_down(engine, market, shares, price):
    size = Decimal(shares)
    proceeds = size * Decimal(price)
    market.sold_down_shares += size
    market.sold_down_proceeds += proceeds
    market.down -= size
    market.net_cash_flow += proceeds
    engine.state.cash += proceeds


def test_pair_floor_holds_the_cheap_side(tmp_path):
    engine = make_engine(
        tmp_path,
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("30"),
        clip_size=Decimal("30"),
        split_clip=Decimal("30"),
        min_order_size=Decimal("5"),
        pair_min_edge=Decimal("0.01"),
        max_unpaired_fraction=Decimal("0.5"),
        auto_split=False,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        min_order_rest_seconds=20,
        close_improve_seconds=90,
        stop_quoting_before_expiry_ms=20_000,
        pre_split_lead_ms=45_000,
        stale_after_ms=1_000_000,
    )
    end = 300_000
    run(
        engine,
        [
            market_event(end_ts=end),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            book_event("m1", "down", "0.84", "0.86", ts=1),
            book_event("m1", "up", "0.03", "0.05", ts=1),
        ],
    )
    market = engine.state.markets["m1"]
    _sell_down(engine, market, "5.65", "0.86")
    floor = pair_edge_floor(market, "up", engine.config)
    assert floor is not None and floor >= Decimal("0.15")
    proposal = propose(engine.state, market, "up", engine.config)
    assert proposal.reason == "ok"
    assert proposal.price >= Decimal("0.15")
    assert proposal.price > Decimal("0.05")
    # Close step-down cannot cut through the floor or through bid + 1 tick.
    engine.state.now_ms = end - 50_000
    eased = engine._ease_for_close(market, proposal)
    assert eased.reason == "ok"
    assert eased.price >= Decimal("0.15")
    assert eased.price >= Decimal("0.03") + market.tick_size
    logs = run(engine, [{"type": "heartbeat", "event_id": "hb", "ts": end - 50_000}])
    posted = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "up" and order.status in {"queued", "live"}
    ]
    assert posted
    # Breakeven is 1 - 0.86. The bid is under it, so the leftover rests there.
    assert all(order.price >= Decimal("0.14") for order in posted)
    assert any(row["kind"] != "pair_floor_block" for row in logs)


def test_unpaired_cap_and_pair_floor_block(tmp_path):
    engine = make_engine(
        tmp_path,
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("30"),
        clip_size=Decimal("30"),
        min_order_size=Decimal("5"),
        pair_min_edge=Decimal("0.01"),
        max_unpaired_fraction=Decimal("0.5"),
        auto_split=False,
        quote_cooldown_ms=0,
    )
    run(
        engine,
        [
            market_event(),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            * [
                book_event("m1", outcome, "0.04", "0.06", ts=1)
                for outcome in ("up", "down")
            ],
            {"type": "heartbeat", "event_id": "hb", "ts": 2},
        ],
    )
    # 0.06 + 0.06 is far under 1.01, so the loss budget still applies.
    # 0.03 * 30 / (1 - 0.06) is about one share, under the 5-share minimum.
    assert engine.state.orders == {}
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert any(json.loads(line)["kind"] == "pair_floor_block" for line in log.splitlines())


def _run5_session(tmp_path, **extra):
    """Capital, fees, and pair edge from the live run that blocked 0.48/0.55."""
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
        min_edge=Decimal("0.01"),
        skew_edge_per_share=Decimal("0"),
        max_spread=Decimal("0.10"),
        auto_split=False,
        merge_when_idle=True,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        fill_mode="none",
        min_order_rest_seconds=20,
        close_improve_seconds=90,
        stop_quoting_before_expiry_ms=20_000,
        pre_split_lead_ms=45_000,
        stale_after_ms=1_000_000,
    )
    values.update(extra)
    return make_engine(tmp_path, **values)


def _working(engine):
    return [
        order
        for order in engine.state.orders.values()
        if order.status in {"queued", "live"}
    ]


def test_fresh_mid_pair_stays_inside_the_loss_budget(tmp_path):
    engine = _run5_session(tmp_path)
    engine.config.merge_when_idle = False
    end = 300_000
    run(
        engine,
        [
            market_event(end_ts=end),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            book_event("m1", "down", "0.46", "0.48", ts=1),
            book_event("m1", "up", "0.53", "0.55", ts=1),
            {"type": "heartbeat", "event_id": "hb", "ts": 2},
        ],
    )
    market = engine.state.markets["m1"]
    budget = Decimal("30") * Decimal("0.03")
    for order in _working(engine):
        assert order.size * (Decimal("1") - order.price) <= budget + Decimal("0.01")
    # 5 * (1 - 0.48) is 2.60, above the 0.90 budget, so the 15-lot does not rest.
    assert engine.state.orders == {}
    assert market.up == Decimal("30") and market.down == Decimal("30")


def test_up_010_blocked_and_016_allowed_after_down_fill(tmp_path):
    engine = _run5_session(tmp_path)
    end = 300_000
    run(
        engine,
        [
            market_event(end_ts=end),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            book_event("m1", "down", "0.84", "0.86", ts=1),
            book_event("m1", "up", "0.08", "0.10", ts=1),
        ],
    )
    market = engine.state.markets["m1"]
    _sell_down(engine, market, "5", "0.86")
    floor = pair_edge_floor(market, "up", engine.config)
    assert floor == Decimal("0.15")
    blocked = propose(engine.state, market, "up", engine.config)
    assert blocked.reason != "ok" or blocked.price >= Decimal("0.15")
    assert not (blocked.reason == "ok" and blocked.price <= Decimal("0.10"))
    run(
        engine,
        [
            book_event("m1", "up", "0.14", "0.16", ts=2),
            {"type": "heartbeat", "event_id": "hb", "ts": 2},
        ],
    )
    # Bid 0.14 is 1 - 0.86, so the filled size is taken instead of left to expire.
    assert market.sold_up_shares == Decimal("5")
    assert market.up == market.down


def test_close_improve_never_goes_below_the_floor(tmp_path):
    engine = _run5_session(tmp_path)
    # The floor check is about price, so give the size budget room to quote.
    engine.config.initial_capital = Decimal("200")
    engine.config.max_loss_fraction = Decimal("0.50")
    end = 300_000
    run(
        engine,
        [
            market_event(end_ts=end),
            {"type": "split", "event_id": "s", "ts": 1, "market_id": "m1", "amount": "30"},
            book_event("m1", "down", "0.40", "0.48", ts=1),
            book_event("m1", "up", "0.46", "0.55", ts=1),
        ],
    )
    market = engine.state.markets["m1"]
    up = propose(engine.state, market, "up", engine.config)
    assert up.reason == "ok" and up.price == Decimal("0.55")
    # Deep in the close window, one tick above the bid (0.47) is under 1.01 - 0.48.
    engine.state.now_ms = 254_000
    eased = engine._ease_for_close(market, up)
    assert eased.reason == "ok"
    assert eased.price >= Decimal("0.53")
    assert eased.price >= market.book("up").best_bid() + market.tick_size

    _sell_down(engine, market, "5", "0.86")
    # Set the book without an event. A book event would sell the leftover leg.
    market.book("down").bids = [(Decimal("0.84"), Decimal("100"))]
    market.book("down").asks = [(Decimal("0.86"), Decimal("10"))]
    market.book("up").bids = [(Decimal("0.08"), Decimal("100"))]
    market.book("up").asks = [(Decimal("0.16"), Decimal("10"))]
    matched = propose(engine.state, market, "up", engine.config)
    assert matched.reason == "ok"
    assert matched.price >= Decimal("0.16")
    eased_matched = engine._ease_for_close(market, matched)
    assert eased_matched.reason == "ok"
    assert eased_matched.price >= Decimal("0.15")
    assert eased_matched.price >= market.book("up").best_bid() + market.tick_size


def test_live_stale_feed_waits_and_spares_a_young_order(tmp_path):
    engine = make_engine(
        tmp_path,
        stale_after_ms=1000,
        stale_feed_seconds=10,
        min_order_rest_seconds=20,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        quote_cooldown_ms=0,
    )
    engine.mode = "live"
    run(
        engine,
        [
            market_event(),
            * [
                book_event("m1", outcome, "0.48", "0.52", ts=0)
                for outcome in ("up", "down")
            ],
            {"type": "heartbeat", "event_id": "hb0", "ts": 0},
            {"type": "clock", "event_id": "live", "ts": 1},
        ],
    )
    assert any(order.status == "live" for order in engine.state.orders.values())
    # 2.7s of quiet used to cancel. Receipt time, not the exchange stamp, counts.
    logs = run(engine, [{"type": "heartbeat", "event_id": "quiet", "ts": 2700}])
    assert not any(row["reason"] == "stale_feed" for row in logs)
    assert any(order.status == "live" for order in engine.state.orders.values())
    # Past 10s, but the order is younger than the 20s rest, so it stays.
    logs = run(engine, [{"type": "heartbeat", "event_id": "old", "ts": 11000}])
    assert any(row["reason"] == "stale_feed" for row in logs)
    assert any(order.status == "live" for order in engine.state.orders.values())
    # A repeated REST snapshot (same hash, new event) refreshes the receipt time.
    refresh = [
        book_event("m1", "up", "0.48", "0.52", ts=12000, digest="book-up"),
        book_event("m1", "down", "0.48", "0.52", ts=12000, digest="book-down"),
    ]
    again = [
        book_event("m1", "up", "0.48", "0.52", ts=12000, digest="book-up"),
        book_event("m1", "down", "0.48", "0.52", ts=12000, digest="book-down"),
    ]
    again[0]["event_id"] = "book-up-rest"
    again[1]["event_id"] = "book-down-rest"
    run(
        engine,
        [
            *refresh,
            *again,
            {"type": "heartbeat", "event_id": "fresh", "ts": 15000},
        ],
    )
    assert any(order.status == "live" for order in engine.state.orders.values())
    # Once the order has rested, a truly quiet feed may cancel it.
    run(engine, [{"type": "heartbeat", "event_id": "rested", "ts": 33000}])
    assert any(
        order.status == "cancel_requested" for order in engine.state.orders.values()
    )


def test_stale_feed_cancels_a_young_order_when_the_bid_trades_through(tmp_path):
    engine = make_engine(
        tmp_path,
        stale_feed_seconds=10,
        min_order_rest_seconds=20,
        ack_latency_ms=0,
        quote_cooldown_ms=0,
    )
    engine.mode = "live"
    run(
        engine,
        [
            market_event(),
            book_event("m1", "up", "0.40", "0.50", ts=0),
            book_event("m1", "down", "0.40", "0.50", ts=0),
            {"type": "heartbeat", "event_id": "hb", "ts": 1},
            {"type": "clock", "event_id": "live", "ts": 2},
        ],
    )
    order = next(item for item in engine.state.orders.values() if item.outcome == "up")
    run(
        engine,
        [
            book_event("m1", "up", str(order.price), "0.99", ts=3000),
            book_event("m1", "down", "0.40", "0.50", ts=3000),
            {"type": "heartbeat", "event_id": "through", "ts": 14000},
        ],
    )
    assert order.status == "cancel_requested"


def test_chain_worker_does_not_block_the_split(tmp_path):
    engine = _rollover_engine(tmp_path)
    engine.chain_events = []
    engine.redeem_pending_path = str(tmp_path / "redeem_pending.json")
    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda text: text)
    started = threading.Event()
    release = threading.Event()

    def submit(creds, action, *args, **kwargs):
        if action != "redeem":
            return ("tx-split", True)
        started.set()
        assert release.wait(2)
        return ("tx-1", True)

    import paper_split_maker.chain_send as chain_send

    original = chain_send.submit_position
    chain_send.submit_position = submit
    notes = []
    worker = ChainWorker(engine, creds, notes)
    engine.chain_worker = worker
    try:
        engine.chain_outbox.append(
            {
                "action": "redeem",
                "market_id": "slow",
                "condition_id": "0x" + "ab" * 32,
                "amount": "1",
            }
        )
        engine.redeem_resolved = lambda condition: True
        began = time.monotonic()
        _flush_chain(engine, creds, notes)
        elapsed = time.monotonic() - began
        assert elapsed < 0.3
        assert started.wait(1)
        # The next split is decided while that redeem is still waiting.
        reader = _Reader()
        stamp = (OPEN0 + 300 - 30) * 1000
        reader.now_ms = stamp
        _consider_pre_split(engine, reader, engine.config, creds, stamp, notes)
        assert any("split submitted" in note for note in notes)
    finally:
        release.set()
        worker.stop.set()
        worker.thread.join(timeout=2)
        chain_send.submit_position = original


def test_short_pusd_balance_shrinks_the_split(tmp_path, monkeypatch):
    import paper_split_maker.chain_send as chain_send

    seen = []

    def submit(creds, action, condition_id, amount, *args, **kwargs):
        seen.append((action, Decimal(str(amount))))
        return ("tx", True)

    monkeypatch.setattr(chain_send, "submit_position", submit)
    monkeypatch.setattr(chain_send, "split_tokens_present", lambda *a, **k: True)
    engine = _rollover_engine(tmp_path)
    engine.collateral_balance = lambda: Decimal("6")
    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda text: text)
    stamp = (OPEN0 + 300 - 20) * 1000
    reader = _Reader()
    reader.now_ms = stamp
    notes = []
    _consider_pre_split(engine, reader, engine.config, creds, stamp, notes)
    splits = [item for item in seen if item[0] == "split"]
    assert splits and splits[0][1] == Decimal("6")
    assert _lead_ms(engine.config) == 45_000


def test_redeem_no_market_found_is_deferred(tmp_path):
    engine = make_engine(tmp_path, redeem_max_attempts=3)
    engine.chain_events = []
    engine.redeem_pending_path = str(tmp_path / "redeem_pending.json")
    engine.redeem_resolved = lambda condition: True
    calls = []

    def submit(*args, **kwargs):
        calls.append(args[1] if len(args) > 1 else "")
        raise RuntimeError("No market found for condition 0xabc")

    import paper_split_maker.chain_send as chain_send

    original = chain_send.submit_position
    chain_send.submit_position = submit
    creds = SimpleNamespace(redact=lambda text: text)
    worker = ChainWorker(engine, creds, [])
    try:
        worker._handle(
            {
                "action": "redeem",
                "market_id": "m1",
                "condition_id": "0x" + "11" * 32,
                "amount": "1",
            }
        )
        assert len(calls) == 1
        assert engine.chain_events[-1]["kind"] == "redeem_deferred"
        saved = json.loads((tmp_path / "redeem_pending.json").read_text(encoding="utf-8"))
        assert saved and saved[0]["condition_id"].startswith("0x")
    finally:
        worker.stop.set()
        worker.thread.join(timeout=2)
        chain_send.submit_position = original


def test_unresolved_market_is_not_redeemed(tmp_path):
    engine = make_engine(tmp_path, redeem_max_attempts=2)
    engine.chain_events = []
    engine.redeem_resolved = lambda condition: False
    calls = []

    import paper_split_maker.chain_send as chain_send

    original = chain_send.submit_position
    chain_send.submit_position = lambda *a, **k: calls.append("called") or ("tx", True)
    creds = SimpleNamespace(redact=lambda text: text)
    worker = ChainWorker(engine, creds, [])
    try:
        item = {
            "action": "redeem",
            "market_id": "m1",
            "condition_id": "0x" + "22" * 32,
            "amount": "1",
        }
        worker._handle(item)
        worker._handle(item)
        assert calls == []
        assert any(event["kind"] == "redeem_deferred" for event in engine.chain_events)
    finally:
        worker.stop.set()
        worker.thread.join(timeout=2)
        chain_send.submit_position = original


def test_resolution_settled_requires_a_payout():
    condition = "0x" + "ab" * 32
    assert resolution_settled(condition, {"data": []}) is False
    assert (
        resolution_settled(
            condition,
            {
                "data": [
                    {
                        "condition_id": condition,
                        "payoutDenominator": "0",
                        "payoutNumerators": ["0", "0"],
                    }
                ]
            },
        )
        is False
    )
    assert (
        resolution_settled(
            condition,
            {
                "data": [
                    {
                        "condition_id": condition,
                        "payout_denominator": "1",
                        "payout_numerators": ["1", "0"],
                    }
                ]
            },
        )
        is True
    )


def test_redeem_pending_subcommand_retries_a_resolved_market(tmp_path):
    path = tmp_path / "redeem_pending.json"
    condition = "0x" + "cd" * 32
    path.write_text(
        json.dumps(
            [{"market_id": "m", "condition_id": condition, "amount": "1"}]
        ),
        encoding="utf-8",
    )
    done = []
    result = redeem_pending_once(
        path,
        submit=lambda record: done.append(record["condition_id"]),
        resolved=lambda condition_id: condition_id == condition,
    )
    assert result["redeemed"] == [condition]
    assert result["left"] == 0
    assert done == [condition]
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_skipped_session_does_not_count_and_duration_is_not_the_cap(tmp_path):
    engine = make_engine(tmp_path)
    run(engine, [market_event()])
    market = engine.state.markets["m1"]
    _note_session_done(engine, market)
    assert getattr(engine, "sessions_finished", set()) == set()
    from paper_split_maker.live_runner import _ensure_live_state

    _ensure_live_state(engine)
    engine.preopen_joined = {market.market_id}
    market.gross_split = Decimal("30")
    market.up = Decimal("0")
    market.down = Decimal("0")
    _note_session_done(engine, market)
    assert market.market_id not in engine.sessions_finished
    engine.sell_accepted.add(market.market_id)
    _note_session_done(engine, market)
    assert market.market_id in engine.sessions_finished
    assert _runtime_seconds(engine.config) == 2400

    clock = SimpleNamespace(now=1_000.0)

    def now():
        return clock.now

    def sleep(seconds):
        clock.now += 5

    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda text: text)
    probe = _rollover_engine(tmp_path / "probe")
    probe.config.max_runtime_seconds = 20
    probe.config.max_open_markets = 3
    summary = run_confirmed_live(
        probe.config,
        creds,
        tmp_path / "out",
        duration_seconds=5,
        max_markets=3,
        client=SimpleNamespace(
            post_heartbeat=lambda heartbeat_id: {"heartbeat_id": "hb"},
            get_open_orders=lambda: [],
            get_order=lambda order_id: {"id": order_id, "size_matched": "0"},
            cancel_orders=lambda ids: None,
            cancel_all=lambda: None,
        ),
        reader=_Reader(),
        now_s=now,
        sleep=sleep,
        poster=lambda client, orders: [
            {"orderID": f"ex-{index}", "success": True} for index, _order in enumerate(orders)
        ],
    )
    assert summary["mode"] == "live"
    # Five seconds of duration must not be the stop. The cap is 20s here.
    assert clock.now >= 1_000 + 20
