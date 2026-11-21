"""Fast paper loop: websocket books, event-driven quotes, and the pair take.

No network, no keys, no orders. The socket test speaks to 127.0.0.1 only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from argparse import Namespace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from paper_split_maker.binance_btc import _read_frame
from paper_split_maker.cli import _apply_strategy, _credential_env
from paper_split_maker.config import Config
from paper_split_maker.feeds import heartbeat_reply
from paper_split_maker.predict_ws import (
    BookCache,
    PredictBookFeed,
    book_topics,
    parse_frame,
    yes_to_events,
)
from paper_split_maker.public_client import PublicReadClient
from paper_split_maker.session import _paper_fast_loop, _watch_once
from paper_split_maker.small_pair import plan_small_pair, shade_losing_touch, take_completion
from tests.helpers import book_event, make_engine, market_event, run

ROOT = Path(__file__).resolve().parents[1]
OPEN = 1_700_000_000
T0 = OPEN * 1000

ORDERBOOK = {
    "type": "M",
    "topic": "predictOrderbook/123",
    "data": {
        "version": 1,
        "marketId": 123,
        "updateTimestampMs": 1736696400000,
        "orderCount": 42,
        "asks": [[0.62, 1500], [0.63, 800]],
        "bids": [[0.61, 2000], [0.6, 1200]],
        "lastOrderSettled": {
            "id": "987654321",
            "price": "0.62",
            "kind": "LIMIT",
            "marketId": 123,
            "side": "Ask",
            "outcome": "Yes",
        },
    },
}


def _engine(tmp_path, **overrides):
    values = dict(
        strategy="small_pair",
        round_budget=Decimal("25"),
        per_market_budget=Decimal("25"),
        stale_after_ms=500_000,
        ack_latency_ms=250,
        cancel_latency_ms=500,
        max_open_markets=4,
        max_loss_fraction=Decimal("0.99"),
    )
    values.update(overrides)
    return make_engine(tmp_path, **values)


def _open(ts: int = T0):
    return market_event(
        slug=f"btc-updown-5m-{OPEN}",
        end_ts=(OPEN + 300) * 1000,
        ts=ts,
    )


def _books(ts: int, bid: str = "0.48", ask: str = "0.52", ask_size: str = "10"):
    events = []
    for outcome in ("up", "down"):
        events.append(
            book_event(
                "m1",
                outcome,
                bid,
                ask,
                ts=ts,
                bid_size="10",
                ask_size=ask_size,
                digest=f"{outcome}-{ts}-{bid}-{ask}-{ask_size}",
            )
        )
    events.append(
        {"type": "heartbeat", "event_id": f"hb-{ts}-{bid}", "ts": ts, "market_id": "m1"}
    )
    return events


def _server_text(payload: bytes) -> bytes:
    header = bytearray([0x81])
    size = len(payload)
    if size < 126:
        header.append(size)
    else:
        header.append(126)
        header.extend(size.to_bytes(2, "big"))
    return bytes(header) + payload


def test_parse_orderbook_and_top_and_ignore_heartbeat():
    parsed = parse_frame(ORDERBOOK)
    assert parsed is not None
    assert parsed["market_id"] == "123"
    assert parsed["kind"] == "book"
    assert parsed["bids"][0]["price"] == "0.61"
    assert parsed["asks"][0]["size"] == "1500"
    assert parsed["last_order_settled"]["id"] == "987654321"
    assert parsed["timestamp"] == 1736696400000
    top = parse_frame(
        {
            "type": "M",
            "topic": "predictTopOfBook/123",
            "data": {
                "marketId": 123,
                "bestBid": [0.6, 50],
                "bestAsk": [0.62, 1500],
            },
        }
    )
    assert top["kind"] == "top"
    assert top["bids"][0] == {"price": "0.6", "size": "50"}
    beat = {"type": "M", "topic": "heartbeat", "data": 1736696400000}
    assert parse_frame(beat) is None
    assert heartbeat_reply(beat) == {"method": "heartbeat", "data": 1736696400000}
    topics = book_topics("123")
    assert topics == ["predictOrderbook/123", "predictTopOfBook/123"]
    assert book_topics("01") == []
    blob = json.dumps(topics)
    assert "api" not in blob


def test_top_of_book_keeps_worse_depth_and_down_is_the_complement():
    cache = BookCache()
    full = parse_frame(
        {
            "type": "M",
            "topic": "predictOrderbook/123",
            "data": {
                "marketId": 123,
                "updateTimestampMs": 1736696400000,
                "bids": [[0.61, 2000], [0.5, 10]],
                "asks": [[0.62, 1500], [0.7, 8]],
            },
        }
    )
    cache.apply(full)
    merged = cache.apply(
        parse_frame(
            {
                "type": "M",
                "topic": "predictTopOfBook/123",
                "data": {"marketId": 123, "bestBid": [0.6, 50], "bestAsk": [0.63, 4]},
            }
        )
    )
    assert merged is not None
    assert [level["price"] for level in merged["bids"]] == ["0.6", "0.5"]
    assert [level["price"] for level in merged["asks"]] == ["0.63", "0.7"]
    assert cache.apply(parse_frame({
        "type": "M",
        "topic": "predictTopOfBook/123",
        "data": {"marketId": 123, "bestBid": [0.6, 50], "bestAsk": [0.63, 4]},
    })) is None
    events = yes_to_events("m1", full, 1_700_000_000_000, 2, "1")
    assert [event["outcome"] for event in events] == ["up", "down"]
    assert events[0]["bids"][0] == ("0.61", "2000")
    assert events[1]["bids"][0][0] == "0.38"
    assert events[1]["asks"][0][0] == "0.39"


def test_local_socket_delivers_a_book_without_logging_the_key():
    seen = {}
    released = threading.Event()

    def serve():
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        seen["port"] = listener.getsockname()[1]
        listener.listen(1)
        listener.settimeout(5)
        released.set()
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            listener.close()
            return
        conn.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                part = conn.recv(4096)
                if not part:
                    break
                data += part
            seen["headers"] = data.decode("latin1", "replace")
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            )
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    opcode, payload = _read_frame(conn)
                except socket.timeout:
                    continue
                if opcode != 1 or not payload:
                    continue
                body = json.loads(payload.decode("utf-8"))
                if body.get("method") == "subscribe":
                    seen["topic"] = body["params"][0]
                    break
            conn.sendall(_server_text(json.dumps(ORDERBOOK).encode("utf-8")))
            time.sleep(0.4)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert released.wait(2)
    feed = PredictBookFeed(f"ws://127.0.0.1:{seen['port']}", "unit-secret")
    feed.start()
    feed.set_markets(["123"])
    rows = []
    deadline = time.time() + 4
    try:
        while time.time() < deadline:
            rows = feed.drain()
            if rows:
                break
            time.sleep(0.05)
    finally:
        feed.stop()
        thread.join(2)
    assert rows, feed.last_error
    assert rows[0]["market_id"] == "123"
    assert rows[0]["bids"][0]["price"] == "0.61"
    assert seen.get("topic") == "predictOrderbook/123"
    assert "x-api-key: unit-secret" in seen["headers"]
    assert "unit-secret" not in repr(feed)
    assert "unit-secret" not in feed.last_error


def test_batched_orderbooks_parse_and_omit_the_key_from_the_url():
    class Transport:
        def __init__(self):
            self.url = ""

        def get_json(self, url: str):
            self.url = url
            return {
                "success": True,
                "data": [
                    {
                        "marketId": 123,
                        "updateTimestampMs": 1736696400000,
                        "bids": [[0.61, 2000]],
                        "asks": [[0.62, 1500]],
                    }
                ],
            }

    transport = Transport()
    client = PublicReadClient(transport, network="mainnet", api_key="unit-secret")
    books = client.fetch_orderbooks(["123", "456"])
    assert "/v1/markets/orderbooks?ids=123&ids=456" in transport.url
    assert "unit-secret" not in transport.url
    assert books["123"]["bids"][0]["price"] == "0.61"
    assert books["123"]["asks"][0]["size"] == "1500"


def test_take_math_and_shade_keeps_the_losing_bid():
    assert take_completion(
        up_shares=Decimal("5"),
        down_shares=Decimal("0"),
        up_cost=Decimal("2.00"),
        down_cost=Decimal("0"),
        up_ask=Decimal("0.55"),
        down_ask=Decimal("0.58"),
        up_ask_size=Decimal("10"),
        down_ask_size=Decimal("10"),
        cap=Decimal("0.99"),
    ) == ("down", Decimal("0.58"), Decimal("5"))
    assert take_completion(
        up_shares=Decimal("5"),
        down_shares=Decimal("0"),
        up_cost=Decimal("2.30"),
        down_cost=Decimal("0"),
        up_ask=Decimal("0.50"),
        down_ask=Decimal("0.55"),
        up_ask_size=Decimal("10"),
        down_ask_size=Decimal("10"),
        cap=Decimal("0.99"),
    ) is None
    assert take_completion(
        up_shares=Decimal("5"),
        down_shares=Decimal("0"),
        up_cost=Decimal("2.30"),
        down_cost=Decimal("0"),
        up_ask=Decimal("0.50"),
        down_ask=Decimal("0.54"),
        up_ask_size=Decimal("10"),
        down_ask_size=Decimal("10"),
        cap=Decimal("1"),
    ) == ("down", Decimal("0.54"), Decimal("5"))
    capped = take_completion(
        up_shares=Decimal("5"),
        down_shares=Decimal("0"),
        up_cost=Decimal("2"),
        down_cost=Decimal("0"),
        up_ask=None,
        down_ask=Decimal("0.50"),
        up_ask_size=Decimal("0"),
        down_ask_size=Decimal("2"),
        cap=Decimal("0.99"),
    )
    assert capped == ("down", Decimal("0.50"), Decimal("2"))
    assert shade_losing_touch(Decimal("0.40"), Decimal("0.62"), Decimal("0.01")) == Decimal("0.40")
    assert shade_losing_touch(Decimal("0.40"), Decimal("0.30"), Decimal("0.01")) == Decimal("0.38")
    common = dict(
        up_bid=Decimal("0.48"),
        up_ask=Decimal("0.52"),
        up_levels=[(Decimal("0.48"), Decimal("10"))],
        down_bid=Decimal("0.48"),
        down_ask=Decimal("0.52"),
        down_levels=[(Decimal("0.48"), Decimal("10"))],
        tick=Decimal("0.01"),
        up_shares=Decimal("0"),
        down_shares=Decimal("0"),
        budget_left=Decimal("25"),
        age_s=30,
        window_s=300,
        fair_up=Decimal("0.30"),
        fair_quote=True,
        binance_filter=True,
    )
    shaded = plan_small_pair(**common, shade_loser=True)
    plain = plan_small_pair(**common, shade_loser=False)
    assert {bid.outcome for bid in shaded.bids} == {"up", "down"}
    shade_up = max(bid.price for bid in shaded.bids if bid.outcome == "up")
    plain_up = max(bid.price for bid in plain.bids if bid.outcome == "up")
    assert shade_up < plain_up
    assert shade_up > 0


def test_fast_quotes_at_second_1_and_the_old_run_waits(tmp_path):
    slow = _engine(
        tmp_path / "slow",
        small_pair_binance=True,
        price_source="combined",
        small_pair_fast=False,
        small_pair_start_seconds=10,
        ack_latency_ms=0,
        cancel_latency_ms=0,
    )
    early = run(slow, [_open(), *_books(T0 + 1_000)])
    assert not any(row["kind"] == "quote_submit" for row in early)
    later = run(slow, _books(T0 + 10_000, bid="0.47", ask="0.53"))
    assert any(row["kind"] == "quote_submit" for row in later)

    fast = _engine(
        tmp_path / "fast",
        small_pair_binance=True,
        price_source="combined",
        small_pair_fast=True,
        small_pair_shade_loser=True,
        small_pair_start_seconds=10,
        small_pair_fast_start_seconds=1,
        ack_latency_ms=0,
        cancel_latency_ms=0,
    )
    logs = run(fast, [_open(), *_books(T0 + 1_000)])
    assert any(row["kind"] == "quote_submit" for row in logs)


def test_taker_fills_after_ack_and_misses_when_the_ask_runs(tmp_path):
    engine = _engine(tmp_path / "hit", small_pair_fast=True, small_pair_take_sum=Decimal("0.99"))
    run(engine, [_open(), *_books(T0 + 2_000, bid="0.40", ask="0.45")])
    market = engine.state.markets["m1"]
    market.bought_up_shares = Decimal("5")
    market.bought_up_cost = Decimal("2")
    market.up = Decimal("5")
    placed = run(
        engine,
        [{"type": "heartbeat", "event_id": "hb-take", "ts": T0 + 2_000, "market_id": "m1"}],
    )
    takers = [order for order in engine.state.orders.values() if order.exchange_id == "taker"]
    assert len(takers) == 1
    assert takers[0].outcome == "down"
    assert takers[0].price == Decimal("0.45")
    assert takers[0].status == "queued"
    assert any(row["kind"] == "quote_submit" and row["reason"] == "taker_take" for row in placed)
    filled = run(
        engine,
        [{"type": "clock", "event_id": "clock-ack", "ts": T0 + 2_250, "market_id": "m1"}],
    )
    assert takers[0].status == "filled"
    assert market.bought_down_shares == Decimal("5")
    assert any(row["kind"] == "fill" and row["reason"] == "taker_take" for row in filled)

    missed = _engine(tmp_path / "miss", small_pair_fast=True, small_pair_take_sum=Decimal("0.99"))
    run(missed, [_open(), *_books(T0 + 2_000, bid="0.40", ask="0.45")])
    market = missed.state.markets["m1"]
    market.bought_up_shares = Decimal("5")
    market.bought_up_cost = Decimal("2")
    market.up = Decimal("5")
    run(
        missed,
        [{"type": "heartbeat", "event_id": "hb-take", "ts": T0 + 2_000, "market_id": "m1"}],
    )
    run(missed, _books(T0 + 2_100, bid="0.20", ask="0.70"))
    order = next(order for order in missed.state.orders.values() if order.exchange_id == "taker")
    assert order.status == "queued"
    run(missed, [{"type": "clock", "event_id": "clock-miss", "ts": T0 + 2_250}])
    assert order.status == "cancel_requested"
    assert market.bought_down_shares == 0


def test_decision_follows_the_book_without_a_poll_wait(tmp_path):
    engine = _engine(tmp_path, ack_latency_ms=0, cancel_latency_ms=0, small_pair_fast=True)
    run(engine, [_open()])
    samples = []
    previous = None
    for step in range(20):
        bid = Decimal("0.40") - Decimal(step) * Decimal("0.01")
        ask = bid + Decimal("0.04")
        ts = T0 + 2_000 + step * 1_000
        started = time.perf_counter()
        logs = run(engine, _books(ts, bid=format(bid, "f"), ask=format(ask, "f")))
        samples.append((time.perf_counter() - started) * 1000)
        quotes = [
            row for row in logs if row["kind"] == "quote_submit" and row["reason"] == "small_pair"
        ]
        if quotes and previous is not None:
            assert quotes[0]["ts"] == ts
            assert Decimal(str(quotes[0]["price"])) <= previous
        if quotes:
            previous = Decimal(str(quotes[0]["price"]))
    worst = max(samples)
    print(f"FAST_DECISION_MS {worst:.3f}")
    assert worst < 250, worst


def test_fast_loop_wakes_on_a_book_and_does_not_sleep_the_poll_interval(tmp_path):
    engine = _engine(
        tmp_path,
        small_pair_fast=True,
        small_pair_fast_start_seconds=1,
        ack_latency_ms=250,
        cancel_latency_ms=500,
    )
    run(engine, [_open()])
    book = {
        "market_id": "123",
        "bids": [{"price": "0.48", "size": "10"}],
        "asks": [{"price": "0.52", "size": "10"}],
        "timestamp": T0 + 2_000,
        "hash": "h1",
    }

    class Feed:
        def __init__(self):
            self.connected = True
            self.markets = []
            self._frames = [book]

        def set_markets(self, ids):
            self.markets = list(ids)

        def drain(self):
            if not self._frames:
                return []
            return [self._frames.pop(0)]

        def stop(self):
            self.connected = False

    class Reader:
        network = "mainnet"
        api_key = ""
        last_discovery = {
            "error": "",
            "rounds": 1,
            "network": "mainnet",
            "series": "btc-updown-5m",
            "http_status": 200,
            "parsed": 1,
            "matched": 1,
            "unparsed": 0,
        }
        _by_condition = {"0xm1": {"market_id": "123", "precision": 2}}

        def begin_poll(self):
            return None

        def discover_btc_updown(self, *args, **kwargs):
            return []

        def fetch_trades(self, *args, **kwargs):
            return {"data": []}

        def fetch_resolution(self, *args, **kwargs):
            return {"data": []}

        def fetch_orderbooks(self, ids):
            return {}

    class Clock:
        def __init__(self):
            self.t = OPEN + 2
            self.sleeps = []

        def __call__(self):
            return self.t

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.t += seconds
            if len(self.sleeps) >= 4:
                (tmp_path / "STOP").write_text("stop", encoding="utf-8")

    feed = Feed()
    clock = Clock()
    summary = _paper_fast_loop(
        engine.config,
        tmp_path,
        engine,
        Reader(),
        clock,
        clock.sleep,
        None,
        0,
        0,
        lambda text: None,
        ["fast paper test"],
        set(),
        True,
        0,
        book_feed=feed,
    )
    assert summary["stop_reason"] == "manual"
    assert engine.state.markets["m1"].bids_posted > 0
    assert feed.markets == ["123"]
    assert engine.book_transport == "ws"
    assert engine.decision_wall_ms > 0
    assert clock.sleeps
    assert max(clock.sleeps) <= 0.05
    live = [
        order
        for order in engine.state.orders.values()
        if order.status in {"live", "queued", "cancel_requested"}
    ]
    assert live


def test_watchdog_stays_quiet_during_startup(tmp_path):
    engine = _engine(tmp_path, small_pair_fast=True)
    engine.decision_wall_ms = 1_000

    class Feed:
        def __init__(self):
            self.dropped = 0

        def drop(self):
            self.dropped += 1

    feed = Feed()
    dumped = []
    assert (
        _watch_once(
            engine,
            feed,
            dumped.append,
            now_ms=10_000,
            started_ms=8_000,
        )
        is False
    )
    assert engine.stalled is False
    assert feed.dropped == 0
    assert dumped == []
    assert _watch_once(engine, feed, dumped.append, now_ms=80_000, started_ms=8_000) is True
    assert feed.dropped == 1


def test_expired_rounds_do_not_block_the_next_session(tmp_path):
    engine = _engine(tmp_path / "held", max_open_markets=1)
    for index, end in enumerate((1_000, 2_000, 3_000)):
        run(
            engine,
            [
                market_event(
                    f"old{index}",
                    end_ts=end,
                    ts=0,
                    event_id=f"open-old{index}",
                )
            ],
        )
        engine.state.markets[f"old{index}"].status = "expired_unresolved"
    opened = run(
        engine,
        [market_event("live", end_ts=20_000, ts=5_000, event_id="open-live")],
    )
    assert "live" in engine.state.markets
    assert not any(row.get("reason") == "max_open_markets" for row in opened)

    fresh = _engine(tmp_path / "past", max_open_markets=1)
    run(fresh, [market_event("old", end_ts=1_000, ts=0, event_id="open-old")])
    opened = run(
        fresh,
        [market_event("live", end_ts=8_000, ts=2_000, event_id="open-live")],
    )
    assert fresh.state.markets["old"].status == "expired_unresolved"
    assert "live" in fresh.state.markets
    assert not any(row.get("reason") == "max_open_markets" for row in opened)


def test_save_state_retries_a_locked_windows_file(tmp_path, monkeypatch):
    import os

    from paper_split_maker.store import save_state

    engine = _engine(tmp_path)
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "state.json is in use")
        return real(src, dst)

    monkeypatch.setattr("paper_split_maker.store.os.replace", flaky)
    save_state(tmp_path / "state.json", engine.state)
    assert calls["n"] == 3
    assert (tmp_path / "state.json").is_file()

    def locked(src, dst):
        raise PermissionError(13, "state.json is in use")

    monkeypatch.setattr("paper_split_maker.store.os.replace", locked)
    run(engine, [_open()])
    assert engine.listener_error is not None
    assert "PermissionError" in engine.listener_error


def test_a_skip_still_draws_a_chart_point(tmp_path):
    from paper_split_maker.app import Hub

    engine = _engine(tmp_path)
    run(engine, [_open(), *_books(T0 + 2_000)])
    engine.state.markets["m1"].last_binance = Decimal("60334")
    hub = Hub(tmp_path / "hub", mode="live", config=engine.config)
    hub.attach(engine)
    hub.ingest(
        {
            "kind": "quote_skipped",
            "ts": T0 + 5_000,
            "market_id": "m1",
            "reason": "stale_feed",
        }
    )
    points = hub.snapshot()["sessions"][0]["points"]
    assert points
    assert points[-1]["s"] >= 0
    assert points[-1]["btc"] == 60334.0


def test_watchdog_dumps_and_requests_a_restart_when_decisions_stop(tmp_path):
    engine = _engine(tmp_path, small_pair_fast=True)
    engine.decision_wall_ms = 1_000

    class Feed:
        def __init__(self):
            self.dropped = 0
            self.connected = True

        def drop(self):
            self.dropped += 1
            self.connected = False

    feed = Feed()
    dumped = []
    assert _watch_once(engine, feed, dumped.append, now_ms=5_000) is True
    assert engine.stalled is True
    assert engine.restart_paper is False
    assert feed.dropped == 1
    assert dumped == [4_000]
    assert _watch_once(engine, feed, dumped.append, now_ms=10_000) is True
    assert engine.restart_paper is True
    engine.decision_wall_ms = 20_000
    assert _watch_once(engine, feed, dumped.append, now_ms=20_500) is False
    assert engine.stalled is False


def test_fast_loop_stops_when_the_watchdog_asks_for_a_restart(tmp_path):
    engine = _engine(tmp_path, small_pair_fast=True)

    class Feed:
        connected = False

        def set_markets(self, ids):
            return None

        def drain(self):
            return []

        def stop(self):
            return None

    class Reader:
        network = "mainnet"
        api_key = ""
        last_discovery = {}
        _by_condition = {}

        def discover_btc_updown(self, *args, **kwargs):
            return []

        def fetch_trades(self, *args, **kwargs):
            return {"data": []}

        def fetch_resolution(self, *args, **kwargs):
            return {"data": []}

        def fetch_orderbooks(self, ids):
            return {}

    box = {}

    def _run():
        box["summary"] = _paper_fast_loop(
            engine.config,
            tmp_path,
            engine,
            Reader(),
            time.time,
            time.sleep,
            None,
            0,
            0,
            lambda text: None,
            ["stall"],
            set(),
            True,
            0,
            book_feed=Feed(),
        )

    thread = threading.Thread(target=_run, name="stall-restart", daemon=True)
    thread.start()
    try:
        started = time.time() + 2
        while time.time() < started and not engine.decision_wall_ms:
            time.sleep(0.05)
        assert engine.decision_wall_ms > 0
        engine.restart_paper = True
        thread.join(3)
    finally:
        engine.restart_paper = True
        (tmp_path / "STOP").write_text("stop", encoding="utf-8")
        thread.join(2)
    assert not thread.is_alive()
    assert box["summary"]["stop_reason"] == "stall"


def test_rollover_and_toggle_do_not_block_the_decision_loop(tmp_path):
    """A blocked discovery GET must not freeze quotes or the bot toggle."""
    from paper_split_maker.app import PaperControl

    engine = _engine(
        tmp_path,
        small_pair_fast=True,
        small_pair_fast_start_seconds=0,
        small_pair_series=("btc-updown-5m",),
        ack_latency_ms=0,
        cancel_latency_ms=0,
        max_open_markets=6,
    )
    now_s = int(time.time())
    open_s = (now_s // 300) * 300
    opened_ms = now_s * 1000
    run(
        engine,
        [
            market_event(
                slug=f"btc-updown-5m-{open_s}",
                end_ts=(open_s + 300) * 1000,
                ts=opened_ms,
                reference_price="60324.875",
            ),
            book_event("m1", "up", "0.48", "0.52", ts=opened_ms + 1, digest="up"),
            book_event("m1", "down", "0.48", "0.52", ts=opened_ms + 1, digest="down"),
        ],
    )
    control = PaperControl(True)
    engine.paper_control = control

    class Feed:
        def __init__(self):
            self.connected = True
            self.markets = []
            self._frames = []
            self.sent = set()

        def set_markets(self, ids):
            self.markets = list(ids)
            for ident in self.markets:
                if ident in self.sent:
                    continue
                self.sent.add(ident)
                self._frames.append(
                    {
                        "market_id": ident,
                        "bids": [{"price": "0.48", "size": "10"}],
                        "asks": [{"price": "0.52", "size": "10"}],
                        "timestamp": int(time.time() * 1000),
                        "hash": f"h-{ident}",
                    }
                )

        def drain(self):
            rows = self._frames
            self._frames = []
            return rows

        def stop(self):
            self.connected = False

    class Reader:
        network = "mainnet"
        api_key = ""

        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls = 0
            self._by_condition = {"0xm1": {"market_id": "123", "precision": 2}}
            self.last_discovery = {
                "error": "",
                "rounds": 1,
                "network": "mainnet",
                "series": "btc-updown-5m",
                "http_status": 200,
                "parsed": 1,
                "matched": 1,
                "unparsed": 0,
                "starts": [],
            }

        def begin_poll(self):
            return None

        def discover_btc_updown(self, now_s, limit, slugs=None, series=None):
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                assert self.release.wait(8), "discovery was not released"
            found = []
            for slug in slugs or []:
                tail = str(slug).rsplit("-", 1)[-1]
                if not tail.isdigit():
                    continue
                found.append(_gamma(int(tail)))
                if len(found) >= int(limit or 1):
                    break
            for payload in found:
                self._by_condition[str(payload["conditionId"]).lower()] = {
                    "market_id": str(700000 + (int(payload["slug"].rsplit("-", 1)[-1]) % 10000)),
                    "precision": 2,
                }
            self.last_discovery = {
                "error": "",
                "rounds": len(found),
                "network": "mainnet",
                "series": series or "btc-updown-5m",
                "http_status": 200,
                "parsed": len(found),
                "matched": len(found),
                "unparsed": 0,
                "starts": [item.get("slug") for item in found],
            }
            return found

        def fetch_trades(self, *args, **kwargs):
            return {"data": []}

        def fetch_resolution(self, *args, **kwargs):
            return {"data": []}

        def fetch_orderbooks(self, ids):
            return {}

    def _buys(status):
        rows = []
        for order in list(engine.state.orders.values()):
            if order.market_id != "m1" or getattr(order, "side", "buy") != "buy":
                continue
            if order.status in status:
                rows.append(order)
        return rows

    reader = Reader()
    box = {}

    def _run():
        box["summary"] = _paper_fast_loop(
            engine.config,
            tmp_path,
            engine,
            reader,
            time.time,
            time.sleep,
            None,
            0,
            0,
            lambda text: None,
            ["rollover toggle"],
            set(),
            True,
            0,
            book_feed=Feed(),
        )

    thread = threading.Thread(target=_run, name="rollover-test", daemon=True)
    thread.start()
    try:
        assert reader.entered.wait(3), "discovery never started"
        fresh = time.time() + 2
        while time.time() < fresh:
            wall = int(engine.decision_wall_ms or 0)
            age = int(time.time() * 1000) - wall
            if wall and age < 1000 and _buys({"live", "queued"}):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                f"loop did not quote during discovery; decision_wall_ms={engine.decision_wall_ms}"
            )
        control.set_enabled(False)
        toggled = time.time() + 2
        while time.time() < toggled and (_buys({"live", "queued"}) or engine.quoting_enabled):
            time.sleep(0.05)
        assert engine.quoting_enabled is False
        assert not _buys({"live", "queued"})
        control.set_enabled(True)
        resumed = time.time() + 2
        while time.time() < resumed and not _buys({"live", "queued"}):
            time.sleep(0.05)
        assert _buys({"live", "queued"})
        age = int(time.time() * 1000) - int(engine.decision_wall_ms or 0)
        assert age < 1000, age
        reader.release.set()
        opened = time.time() + 3
        while time.time() < opened:
            extra = [market for market in engine.state.markets.values() if market.market_id != "m1"]
            if extra:
                break
            time.sleep(0.05)
        extra = [market for market in engine.state.markets.values() if market.market_id != "m1"]
        assert extra, "next session was not opened after discovery returned"
        assert any(market.status == "open" for market in extra)
    finally:
        reader.release.set()
        (tmp_path / "STOP").write_text("stop", encoding="utf-8")
        thread.join(4)
    assert not thread.is_alive()
    assert box["summary"]["stop_reason"] == "manual"


def _gamma(open_s: int) -> dict:
    slug = f"btc-updown-5m-{open_s}"
    end = datetime.fromtimestamp(open_s + 300, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": slug,
        "version": "v1",
        "conditionId": f"0x{open_s:040x}",
        "slug": slug,
        "question": slug,
        "outcomes": ["Up", "Down"],
        "clobTokenIds": [f"{open_s}-up", f"{open_s}-down"],
        "endDate": end,
        "acceptingOrders": True,
        "closed": False,
        "orderMinSize": "5",
        "orderPriceMinTickSize": "0.01",
    }


def test_rollover_rest_book_fills_the_dashboard(tmp_path):
    """A new round whose socket snapshot has no touch still gets the poll book.

    The chart, the price to beat, and the ladder read that book. Bids post
    while the bot is on.
    """
    from paper_split_maker.app import Hub

    round_s = 1_700_000_100
    engine = _engine(
        tmp_path,
        small_pair_fast=True,
        small_pair_fast_start_seconds=1,
        small_pair_binance=True,
        price_source="combined",
        max_open_markets=1,
        ack_latency_ms=0,
        cancel_latency_ms=0,
    )

    class Px:
        def latest(self):
            return (int(engine.state.now_ms or 0), Decimal("60495.55"))

        def price_at(self, ts_ms):
            return Decimal("60495.55")

        def vol(self, now_ms):
            return None

    engine.binance_feed = Px()
    run(
        engine,
        [
            market_event(
                "m1",
                slug=f"btc-updown-5m-{round_s}",
                end_ts=(round_s + 300) * 1000,
                ts=round_s * 1000,
                event_id="open-a",
            ),
            * [
                book_event(
                    "m1",
                    outcome,
                    "0.48",
                    "0.52",
                    ts=round_s * 1000 + 2_000,
                    bid_size="10",
                    digest=f"a-{outcome}",
                )
                for outcome in ("up", "down")
            ],
        ],
    )
    hub = Hub(tmp_path / "hub", mode="live", config=engine.config)
    hub.attach(engine)

    class Feed:
        def __init__(self):
            self.connected = True
            self.markets = []
            self._sent = False

        def set_markets(self, ids):
            self.markets = list(ids)

        def drain(self):
            if self._sent:
                return []
            self._sent = True
            return [
                {
                    "market_id": "999",
                    "bids": [],
                    "asks": [],
                    "timestamp": 1,
                    "hash": "empty",
                }
            ]

        def stop(self):
            self.connected = False

    class Reader:
        network = "mainnet"
        api_key = ""
        last_discovery = {
            "error": "",
            "rounds": 1,
            "network": "mainnet",
            "series": "btc-updown-5m",
            "http_status": 200,
            "parsed": 1,
            "matched": 1,
            "unparsed": 0,
        }
        _by_condition = {}

        def begin_poll(self):
            return None

        def discover_btc_updown(self, now_s, limit, slugs=None, series=None):
            found = []
            for slug in slugs or []:
                tail = str(slug).rsplit("-", 1)[-1]
                if not tail.isdigit():
                    continue
                open_s = int(tail)
                if open_s < round_s + 300:
                    continue
                payload = _gamma(open_s)
                self._by_condition[str(payload["conditionId"]).lower()] = {
                    "market_id": "999",
                    "precision": 2,
                    "tick": "0.01",
                }
                found.append(payload)
                if len(found) >= max(int(limit or 1), 1):
                    break
            return found

        def fetch_book(self, token):
            token = str(token)
            bid, ask = ("0.46", "0.54") if token.endswith("up") else ("0.44", "0.52")
            return {
                "timestamp": "1",
                "hash": token,
                "bids": [{"price": bid, "size": "20"}],
                "asks": [{"price": ask, "size": "20"}],
                "asset_id": token,
                "tick_size": "0.01",
                "min_order_size": "1",
            }

        def fetch_trades(self, *args, **kwargs):
            return {"data": []}

        def fetch_resolution(self, *args, **kwargs):
            return {"data": []}

        def fetch_orderbooks(self, ids):
            return {}

    class Clock:
        def __init__(self):
            self.t = round_s + 20
            self.n = 0

        def __call__(self):
            return self.t

        def sleep(self, seconds):
            self.n += 1
            if self.n == 4:
                self.t = round_s + 301
            else:
                self.t += 0.05
            if self.n >= 40:
                (tmp_path / "STOP").write_text("stop", encoding="utf-8")

    clock = Clock()
    summary = _paper_fast_loop(
        engine.config,
        tmp_path,
        engine,
        Reader(),
        clock,
        clock.sleep,
        None,
        0,
        0,
        lambda text: None,
        ["rollover"],
        set(),
        True,
        0,
        book_feed=Feed(),
    )
    assert summary["stop_reason"] == "manual"
    nxt = f"btc-updown-5m-{round_s + 300}"
    market = next(item for item in engine.state.markets.values() if item.slug == nxt)
    assert market.bids_posted > 0
    assert market.up_book.best_bid() == Decimal("0.46")
    assert market.down_book.best_bid() == Decimal("0.44")
    assert market.reference_price == Decimal("60495.55")
    assert market.reference_source == "combined"
    snap = hub.snapshot()
    session = next(item for item in snap["sessions"] if item["slug"] == nxt)
    assert session["price_to_beat"] == "60495.55"
    assert session["books"]["up"]["bids"]
    assert session["books"]["up"]["asks"]
    assert session["books"]["down"]["bids"]
    priced = [point for point in session["points"] if point["up"] is not None and point["down"] is not None]
    assert priced


def test_fast_flag_defaults_off_and_dashboard_env_is_forwarded(tmp_path, monkeypatch):
    assert Config().small_pair_fast is False
    config = Config()
    args = Namespace(
        strategy=None,
        series=None,
        binance_lean=False,
        price_source=None,
        pair_gate=None,
        catch_up=None,
        first_leg_seconds=None,
        fast=True,
        capital=None,
        max_loss_fraction=None,
    )
    _apply_strategy(config, args)
    assert config.small_pair_fast is True
    assert config.small_pair_shade_loser is True

    monkeypatch.delenv("PREDICT_NETWORK", raising=False)
    monkeypatch.delenv("PREDICT_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "PREDICT_NETWORK=mainnet\nPREDICT_API_KEY=unit-secret\n",
        encoding="utf-8",
    )
    env = _credential_env(tmp_path / "config.paper.json")
    assert env["PREDICT_NETWORK"] == "mainnet"
    assert env["PREDICT_API_KEY"] == "unit-secret"

    captured = {}

    def fake_run(config, out, duration_seconds, max_markets, env=None, on_engine=None):
        captured["network"] = (env or {}).get("PREDICT_NETWORK")
        captured["has_key"] = bool((env or {}).get("PREDICT_API_KEY"))
        return {"stop_reason": "manual"}

    monkeypatch.setattr("paper_split_maker.app.run_paper", fake_run)
    from paper_split_maker.app import Hub, demo_config

    hub = Hub(
        tmp_path / "hub",
        mode="demo",
        config=demo_config(),
        env={"PREDICT_NETWORK": "mainnet", "PREDICT_API_KEY": "unit-secret"},
    )
    hub.start_live()
    assert hub.thread is not None
    hub.thread.join(2)
    assert captured["network"] == "mainnet"
    assert captured["has_key"] is True
    blob = json.dumps(hub.snapshot())
    assert "unit-secret" not in blob
    assert "PREDICT_" not in blob
    assert "feed" in hub.snapshot()

    fast_script = (ROOT / "run_app_fast.ps1").read_text(encoding="utf-8")
    slow_script = (ROOT / "run_app_combo.ps1").read_text(encoding="utf-8")
    page = (ROOT / "paper_split_maker" / "app_page.html").read_text(encoding="utf-8")
    assert "--port 8767" in fast_script and "--fast" in fast_script and "app_fast" in fast_script
    assert "--no-fast" in slow_script and "8766" in slow_script
    assert 'id="latency"' in page
    assert 'id="status"' in page
    assert 'id="stalled"' in page
    assert "currentOpen" in page
    assert "STALLED" in page
