"""Paper dual_bid. No network, no keys, no orders."""

from __future__ import annotations

import json
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

from paper_split_maker.cli import _apply_strategy, main
from paper_split_maker.config import Config, load_config
from paper_split_maker.dual_bid import allow_bid, clip_for, plan_bids
from paper_split_maker.public_client import PublicReadClient
from paper_split_maker.session import run_paper
from tests.helpers import book_event, make_engine, market_event, run

ROOT = Path(__file__).resolve().parents[1]
TAPE = json.loads(
    (ROOT / "tests" / "fixtures" / "dual_bid_tape.json").read_text(encoding="utf-8")
)
ROUNDS = json.loads(
    (ROOT / "tests" / "fixtures" / "btc_updown_5m_rounds.json").read_text(
        encoding="utf-8"
    )
)
OPEN = int(TAPE["open_unix"])
T0 = OPEN * 1000
END = (OPEN + 300) * 1000
QUOTE = T0 + 8_000


def _engine(tmp_path, **overrides):
    values = dict(
        strategy="dual_bid",
        dual_bid_levels=6,
        dual_bid_requote_ms=1500,
        round_budget=Decimal("40"),
        per_market_budget=Decimal("40"),
        target_pair_cost=Decimal("0.97"),
        stale_after_ms=500_000,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        auto_split=True,
        merge_when_idle=True,
    )
    values.update(overrides)
    return make_engine(tmp_path, **values)


def _open(ts: int = T0) -> dict:
    return market_event(
        slug=f"btc-updown-5m-{OPEN}",
        end_ts=END,
        ts=ts,
    )


def _quote(engine, ts: int, bid: str = "0.48", ask: str = "0.52", bid_size: str = "80"):
    events = [
        book_event(
            "m1",
            "up",
            bid,
            ask,
            ts=ts,
            bid_size=bid_size,
            ask_size="10",
            digest=f"up-{ts}-{bid}-{bid_size}",
        ),
        book_event(
            "m1",
            "down",
            bid,
            ask,
            ts=ts,
            bid_size=bid_size,
            ask_size="10",
            digest=f"down-{ts}-{bid}-{bid_size}",
        ),
        {
            "type": "heartbeat",
            "event_id": f"hb-{ts}",
            "ts": ts,
            "market_id": "m1",
        },
    ]
    return run(engine, events)


def _bids(engine):
    return [
        order
        for order in engine.state.orders.values()
        if order.side == "buy" and order.status in {"queued", "live", "cancel_requested"}
    ]


def test_pair_gates_and_clip():
    assert clip_for(Decimal("0.48"), Decimal("5"), Decimal("24")) == Decimal("5")
    assert clip_for(Decimal("0.10"), Decimal("5"), Decimal("24")) == Decimal("10")
    assert clip_for(Decimal("0.04"), Decimal("5"), Decimal("24")) is None
    held = dict(
        up_shares=Decimal("10"),
        up_cost=Decimal("4"),
        down_shares=Decimal("0"),
        down_cost=Decimal("0"),
        target_pair_cost=Decimal("0.97"),
        pair_cost_stop=Decimal("1"),
        min_share_balance=Decimal("0.85"),
        min_price=Decimal("0.10"),
    )
    assert allow_bid("down", Decimal("0.50"), **held) == "ok"
    assert allow_bid("down", Decimal("0.60"), **held) == "pair_cost"
    assert allow_bid("up", Decimal("0.40"), **held) == "imbalance"
    assert allow_bid("down", Decimal("0.09"), **held) == "below_min_price"
    balanced = dict(held)
    balanced.update(
        up_shares=Decimal("10"),
        up_cost=Decimal("6"),
        down_shares=Decimal("5"),
        down_cost=Decimal("2"),
    )
    assert allow_bid("up", Decimal("0.40"), **balanced) == "pair_cost_stop"


def test_no_bids_before_the_round_or_below_ten_cents(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open()])
    _quote(engine, T0 + 4_000)
    assert _bids(engine) == []
    assert engine.state.markets["m1"].gross_split == 0
    late = _engine(tmp_path / "low")
    run(late, [_open()])
    _quote(late, QUOTE, bid="0.08", ask="0.12", bid_size="100")
    assert _bids(late) == []


def test_ladder_joins_behind_the_displayed_queue(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open()])
    _quote(engine, QUOTE, bid_size=TAPE["book"]["bid_size"])
    up = sorted(
        (order for order in _bids(engine) if order.outcome == "up"),
        key=lambda order: -order.price,
    )
    assert len(up) == 6
    assert up[0].price == Decimal(TAPE["book"]["bid"])
    assert up[0].size == Decimal("5")
    assert up[0].queue_ahead == Decimal(TAPE["book"]["bid_size"])
    assert up[-1].price == Decimal("0.43")
    assert engine.state.markets["m1"].gross_split == 0
    assert all(order.side == "buy" for order in _bids(engine))


def test_recorded_tape_fills_only_after_the_queue(tmp_path):
    engine = _engine(tmp_path, dual_bid_levels=1)
    run(engine, [_open()])
    _quote(engine, QUOTE, bid_size=TAPE["book"]["bid_size"])
    queue = TAPE["queue_trade"]
    fill = TAPE["fill_trade"]
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "queue",
                "ts": QUOTE,
                "market_id": "m1",
                "outcome": queue["outcome"],
                "price": queue["price"],
                "size": queue["size"],
                "taker_side": queue["taker_side"],
                "trade_id": "queue",
            }
        ],
    )
    market = engine.state.markets["m1"]
    assert market.bought_up_shares == 0
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "fill",
                "ts": QUOTE,
                "market_id": "m1",
                "outcome": fill["outcome"],
                "price": fill["price"],
                "size": fill["size"],
                "taker_side": fill["taker_side"],
                "trade_id": "fill",
            }
        ],
    )
    assert market.bought_up_shares == Decimal("5")
    assert market.buy_up_fills == 1
    assert market.bought_up_cost == Decimal("2.4")
    buy = {
        "type": "trade",
        "event_id": "buy-print",
        "ts": QUOTE,
        "market_id": "m1",
        "outcome": "down",
        "price": "0.48",
        "size": "100",
        "taker_side": "BUY",
        "trade_id": "buy-print",
    }
    before = market.down
    run(engine, [buy])
    assert market.down == before
    through = TAPE["through_trade"]
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "through",
                "ts": QUOTE,
                "market_id": "m1",
                "outcome": through["outcome"],
                "price": through["price"],
                "size": through["size"],
                "taker_side": through["taker_side"],
                "trade_id": "through",
            }
        ],
    )
    assert market.bought_down_shares == Decimal("5")
    summary = engine.summary()
    row = summary["rounds"][0]
    assert row["fills_up"] == 1
    assert row["fills_down"] == 1
    assert row["matched_shares"] == "5"
    assert row["pair_cost"] == "0.96"
    assert row["excess_shares"] == "0"
    assert row["pnl"] is None
    assert summary["round_totals"]["fills_up"] == 1


def test_reprices_on_the_cadence_and_pulls_at_the_stop(tmp_path):
    engine = _engine(tmp_path, dual_bid_levels=1)
    run(engine, [_open()])
    _quote(engine, QUOTE)
    _quote(engine, QUOTE + 1000, bid="0.46", ask="0.50")
    live = [order for order in _bids(engine) if order.status in {"queued", "live"}]
    assert {order.price for order in live} == {Decimal("0.48")}
    _quote(engine, QUOTE + 2000, bid="0.46", ask="0.50")
    prices = {order.price for order in _bids(engine) if order.status in {"queued", "live"}}
    assert Decimal("0.46") in prices
    assert all(
        order.status == "cancel_requested"
        for order in engine.state.orders.values()
        if order.price == Decimal("0.48")
    )
    _quote(engine, T0 + 275_000, bid="0.46", ask="0.50")
    assert _bids(engine) == [] or all(
        order.status == "cancel_requested" for order in _bids(engine)
    )
    run(
        engine,
        [
            {
                "type": "heartbeat",
                "event_id": "after-stop",
                "ts": T0 + 275_000,
                "market_id": "m1",
            }
        ],
    )
    assert _bids(engine) == []
    assert engine.state.markets["m1"].gross_split == 0


def test_round_budget_and_loss_floor(tmp_path):
    tight = _engine(tmp_path, dual_bid_levels=6, round_budget=Decimal("3"))
    run(tight, [_open()])
    _quote(tight, QUOTE)
    assert len(_bids(tight)) == 1
    blocked = _engine(
        tmp_path / "floor",
        initial_capital=Decimal("20"),
        max_loss_fraction=Decimal("0.10"),
        per_market_budget=Decimal("20"),
        round_budget=Decimal("20"),
    )
    run(blocked, [_open()])
    _quote(blocked, QUOTE)
    assert _bids(blocked) == []


def test_hold_redeem_pnl(tmp_path):
    engine = _engine(tmp_path, dual_bid_levels=1)
    run(engine, [_open()])
    _quote(engine, QUOTE, bid_size="1")
    for outcome, trade_id in (("up", "buy-up"), ("down", "buy-down")):
        run(
            engine,
            [
                {
                    "type": "trade",
                    "event_id": trade_id,
                    "ts": QUOTE,
                    "market_id": "m1",
                    "outcome": outcome,
                    "price": "0.48",
                    "size": "6",
                    "taker_side": "SELL",
                    "trade_id": trade_id,
                }
            ],
        )
    run(
        engine,
        [
            {
                "type": "resolution",
                "event_id": "resolved",
                "ts": T0 + 200_000,
                "market_id": "m1",
                "source": "data-api-v2",
                "status": "resolved",
                "payouts": [1_000_000, 0],
                "market_type": "BINARY",
            }
        ],
    )
    market = engine.state.markets["m1"]
    assert market.status == "redeemed"
    assert market.winner == "up"
    assert market.net_cash_flow == Decimal("0.2")
    assert engine.state.realized_pnl == Decimal("0.2")
    row = engine.summary()["rounds"][0]
    assert row["winner"] == "up"
    assert Decimal(row["pnl"]) == Decimal("0.2")
    assert Decimal(row["balance"]) == Decimal("1")
    assert Decimal(engine.summary()["round_totals"]["pnl"]) == Decimal("0.2")


def test_plan_skips_the_heavy_side():
    bids = plan_bids(
        up_bids=[(Decimal("0.40"), Decimal("10"))],
        up_ask=Decimal("0.44"),
        down_bids=[(Decimal("0.40"), Decimal("10"))],
        down_ask=Decimal("0.44"),
        tick=Decimal("0.01"),
        levels=3,
        clip=Decimal("5"),
        max_clip=Decimal("24"),
        min_price=Decimal("0.10"),
        up_shares=Decimal("10"),
        up_cost=Decimal("4"),
        down_shares=Decimal("5"),
        down_cost=Decimal("2"),
        target_pair_cost=Decimal("0.97"),
        pair_cost_stop=Decimal("1"),
        min_share_balance=Decimal("0.85"),
        budget_left=Decimal("25"),
    )
    assert bids
    assert all(bid.outcome == "down" for bid in bids)


def test_dual_bid_loads_the_open_round(tmp_path):
    class _Rounds:
        def get_json(self, url: str):
            return ROUNDS

    client = PublicReadClient(_Rounds(), network="mainnet", api_key="not-a-real-key")
    clock = {"t": 1_700_000_000.0}
    summary = run_paper(
        Config(
            strategy="dual_bid",
            split_lead_seconds=300,
            poll_interval_seconds=2,
            max_open_markets=3,
        ),
        tmp_path / "paper",
        duration_seconds=2,
        max_markets=5,
        client=client,
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    text = json.dumps(summary)
    assert "not-a-real-key" not in text
    assert "btc-updown-5m-1699999800" in text
    assert any("dual_bid quotes from +8s until +275s" in note for note in summary["notes"])
    assert not any("already started" in note for note in summary["notes"])
    assert summary["rounds"]
    assert summary["round_totals"]["fills_up"] == 0


def test_old_exchange_timestamp_still_quotes(tmp_path):
    """A snapshot received just now is fresh even when updateTimestampMs is old."""
    engine = _engine(tmp_path, dual_bid_levels=1, stale_after_ms=10_000)
    run(engine, [_open()])
    events = []
    for outcome in ("up", "down"):
        event = book_event(
            "m1",
            outcome,
            "0.48",
            "0.52",
            ts=QUOTE,
            bid_size="80",
            digest=f"{outcome}-stale-stamp",
        )
        event["book_ts"] = QUOTE - 60_000
        events.append(event)
    events.append(
        {"type": "heartbeat", "event_id": "hb-quote", "ts": QUOTE, "market_id": "m1"}
    )
    run(engine, events)
    assert _bids(engine)
    assert engine.state.markets["m1"].bids_posted >= 1
    run(
        engine,
        [
            {
                "type": "heartbeat",
                "event_id": "hb-stale",
                "ts": QUOTE + 11_000,
                "market_id": "m1",
            }
        ],
    )
    market = engine.state.markets["m1"]
    assert market.skip_counts.get("stale_feed", 0) >= 1
    row = engine.summary()["rounds"][0]
    assert row["bids_posted"] >= 1
    assert row["bids_skipped"]["stale_feed"] >= 1


def test_paper_refetches_a_stale_mainnet_book(tmp_path, capsys):
    """The first snapshot must not be reused for the rest of the round."""
    book = json.loads(
        (ROOT / "tests" / "fixtures" / "predict_orderbook.json").read_text(
            encoding="utf-8"
        )
    )
    rounds = json.loads(
        (ROOT / "tests" / "fixtures" / "btc_updown_5m_rounds.json").read_text(
            encoding="utf-8"
        )
    )

    class _Transport:
        def __init__(self):
            self.orderbooks = 0
            self.urls = []

        def get_json(self, url: str):
            self.urls.append(url)
            if "/rounds" in url:
                return rounds
            if url.endswith("/matches") or "/matches?" in url:
                if self.orderbooks >= 2:
                    return {
                        "data": [
                            {
                                "id": "trade-1",
                                "token_id": "111",
                                "side": "BUY",
                                "price": "0.55",
                                "size": "4",
                                "timestamp": 1_700_000_000_000,
                            }
                        ]
                    }
                return {"data": []}
            if "/orderbook" in url:
                self.orderbooks += 1
                payload = json.loads(json.dumps(book))
                if self.orderbooks >= 5:
                    payload["data"]["bids"] = [[0.40, 90], [0.39, 40]]
                    payload["data"]["asks"] = [[0.46, 20]]
                return payload
            if "/markets/" in url:
                return {"data": {"id": 9001, "status": "REGISTERED"}}
            raise AssertionError(url)

    client = PublicReadClient(
        _Transport(), network="mainnet", api_key="not-a-real-key"
    )
    clock = {"t": 1_700_000_000.0}
    summary = run_paper(
        Config(
            strategy="dual_bid",
            split_lead_seconds=300,
            poll_interval_seconds=2,
            stale_after_ms=10_000,
            max_open_markets=3,
            dual_bid_levels=1,
            dual_bid_requote_ms=1500,
            round_budget=Decimal("40"),
            per_market_budget=Decimal("40"),
        ),
        tmp_path / "paper",
        duration_seconds=6,
        max_markets=5,
        client=client,
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    assert client.transport.orderbooks >= 6
    assert "not-a-real-key" not in json.dumps(summary)
    current = next(
        row for row in summary["rounds"] if row["slug"] == "btc-updown-5m-1699999800"
    )
    assert current["books_ok"] >= 2
    assert current["books_failed"] == 0
    assert current["bids_posted"] >= 1
    assert current["trades_seen"] >= 1
    assert current["bids_skipped"].get("stale_feed", 0) == 0
    assert summary["round_totals"]["bids_posted"] >= 1
    state = json.loads((tmp_path / "paper" / "state.json").read_text(encoding="utf-8"))
    prices = [
        order["price"]
        for order in state["orders"].values()
        if order["market_id"] == "btc-updown-5m-1699999800" and order["side"] == "buy"
    ]
    assert "0.4" in prices or "0.40" in prices
    err = capsys.readouterr().err
    assert "books_ok=" in err and "bids_posted=" in err and "trades_seen=" in err


def test_zero_update_timestamp_still_quotes(tmp_path):
    rounds = json.loads(
        (ROOT / "tests" / "fixtures" / "btc_updown_5m_rounds.json").read_text(
            encoding="utf-8"
        )
    )
    book = {
        "success": True,
        "data": {
            "marketId": 9001,
            "updateTimestampMs": 0,
            "lastOrderSettled": None,
            "bids": [[0.48, 80]],
            "asks": [[0.52, 10]],
        },
    }

    class _Transport:
        def get_json(self, url: str):
            if "/rounds" in url:
                return rounds
            if "/orderbook" in url:
                return book
            if "/matches" in url:
                return {"data": []}
            if "/markets/" in url:
                return {"data": {"id": 9001, "status": "REGISTERED"}}
            raise AssertionError(url)

    client = PublicReadClient(_Transport(), network="mainnet", api_key="not-a-real-key")
    clock = {"t": 1_700_000_000.0}
    summary = run_paper(
        Config(
            strategy="dual_bid",
            split_lead_seconds=0,
            poll_interval_seconds=2,
            stale_after_ms=10_000,
            max_open_markets=1,
            dual_bid_levels=1,
            round_budget=Decimal("40"),
            per_market_budget=Decimal("40"),
        ),
        tmp_path / "zero",
        duration_seconds=2,
        max_markets=1,
        client=client,
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    current = next(
        row for row in summary["rounds"] if row["slug"] == "btc-updown-5m-1699999800"
    )
    assert current["books_failed"] == 0
    assert current["books_ok"] >= 2
    assert current["bids_posted"] >= 1


def test_strategy_flag_and_live_refusal(tmp_path, capsys):
    config = load_config(ROOT / "config.paper.json")
    assert config.strategy == "ladder"
    updated = _apply_strategy(config, Namespace(strategy="dual_bid"))
    assert updated.strategy == "dual_bid"
    code = main(
        [
            "--config",
            str(ROOT / "config.paper.json"),
            "--out",
            str(tmp_path / "live"),
            "--strategy",
            "dual_bid",
            "--live",
        ]
    )
    assert code == 2
    assert "paper-only" in capsys.readouterr().out
