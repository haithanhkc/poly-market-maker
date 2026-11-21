"""Fast paper loop: websocket books, event-driven quotes, and the pair take.

No network, no keys, no orders. The socket test speaks to 127.0.0.1 only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from argparse import Namespace
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
from paper_split_maker.session import _paper_fast_loop
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
