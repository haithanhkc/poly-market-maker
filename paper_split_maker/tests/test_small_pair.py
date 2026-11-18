"""Paper small_pair. No network, no keys, no orders."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from paper_split_maker.cli import _apply_strategy, _load, main
from paper_split_maker.config import Config
from paper_split_maker.envfile import derive_from_split, prepare_run
from paper_split_maker.public_client import PublicReadClient
from paper_split_maker.session import run_paper, sessions_due
from paper_split_maker.small_pair import lot_for, plan_small_pair, quiet_after
from tests.helpers import book_event, make_engine, market_event, run

ROOT = Path(__file__).resolve().parents[1]
ROUNDS = json.loads(
    (ROOT / "tests" / "fixtures" / "btc_updown_5m_rounds.json").read_text(
        encoding="utf-8"
    )
)
OPEN = 1_700_000_000
T0 = OPEN * 1000
QUOTE = T0 + 2_000


def _engine(tmp_path, **overrides):
    values = dict(
        strategy="small_pair",
        round_budget=Decimal("25"),
        per_market_budget=Decimal("25"),
        stale_after_ms=500_000,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        max_open_markets=4,
    )
    values.update(overrides)
    return make_engine(tmp_path, **values)


def _open(window: int = 300, ts: int = T0):
    series = "btc-updown-5m" if window <= 300 else "btc-updown-15m"
    return market_event(
        slug=f"{series}-{OPEN}",
        end_ts=(OPEN + window) * 1000,
        ts=ts,
    )


def _books(ts: int, bid: str = "0.48", ask: str = "0.52", bid_size: str = "10"):
    events = []
    for outcome in ("up", "down"):
        events.append(
            book_event(
                "m1",
                outcome,
                bid,
                ask,
                ts=ts,
                bid_size=bid_size,
                ask_size="10",
                digest=f"{outcome}-{ts}-{bid}-{ask}-{bid_size}",
            )
        )
    events.append(
        {"type": "heartbeat", "event_id": f"hb-{ts}", "ts": ts, "market_id": "m1"}
    )
    return events


def _redeem(engine):
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
            },
            {
                "type": "heartbeat",
                "event_id": "hb-redeem",
                "ts": T0 + 200_000,
                "market_id": "m1",
            },
        ],
    )


def _live(engine, outcome: str | None = None):
    orders = [
        order
        for order in engine.state.orders.values()
        if order.side == "buy" and order.status in {"queued", "live"}
    ]
    if outcome is None:
        return orders
    return [order for order in orders if order.outcome == outcome]


def test_lot_and_centred_ladder_sum_to_target():
    assert lot_for(Decimal("0.48")) == Decimal("3")
    assert lot_for(Decimal("0.20")) == Decimal("5")
    assert lot_for(Decimal("0.13")) == Decimal("8")
    assert lot_for(Decimal("0.10")) is None
    assert quiet_after(300) == 270
    assert quiet_after(900) == 840
    plan = plan_small_pair(
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
        age_s=2,
        window_s=300,
    )
    assert plan.reason == ""
    ups = [bid for bid in plan.bids if bid.outcome == "up"]
    downs = [bid for bid in plan.bids if bid.outcome == "down"]
    assert len(ups) == 4
    assert len(downs) == 4
    assert ups[0].price + downs[0].price == Decimal("0.96")
    assert [bid.price for bid in ups] == [
        Decimal("0.48"),
        Decimal("0.47"),
        Decimal("0.46"),
        Decimal("0.45"),
    ]
    assert all(bid.size == Decimal("3") for bid in plan.bids)
    assert all(bid.price < Decimal("0.52") for bid in plan.bids)
    tight = plan_small_pair(
        up_bid=Decimal("0.47"),
        up_ask=Decimal("0.48"),
        up_levels=[],
        down_bid=Decimal("0.47"),
        down_ask=Decimal("0.48"),
        down_levels=[],
        tick=Decimal("0.01"),
        up_shares=Decimal("0"),
        down_shares=Decimal("0"),
        budget_left=Decimal("25"),
        age_s=10,
        window_s=300,
    )
    assert tight.bids
    assert all(
        bid.price < Decimal("0.48") for bid in tight.bids
    )


def test_quotes_four_levels_from_second_two_and_not_before(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open()])
    run(engine, _books(T0 + 1_000))
    assert _live(engine) == []
    assert engine.state.markets["m1"].skip_counts.get("before_open", 0) >= 1
    run(engine, _books(QUOTE))
    live = _live(engine)
    assert len(live) == 8
    prices = {order.price for order in live}
    assert Decimal("0.48") in prices
    assert Decimal("0.45") in prices
    assert all(order.side == "buy" for order in engine.state.orders.values())
    assert engine.state.markets["m1"].gross_split == 0
    assert engine.state.markets["m1"].quoted_as == "small_pair"


def test_imbalance_pulls_the_heavy_side_and_resumes(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open(), *_books(QUOTE)])
    market = engine.state.markets["m1"]
    market.bought_up_shares = Decimal("4")
    market.bought_up_cost = Decimal("1.92")
    run(engine, _books(QUOTE + 2_000))
    assert _live(engine, "up") == []
    assert _live(engine, "down")
    assert market.skip_counts.get("imbalance", 0) >= 1
    market.bought_up_shares = Decimal("1")
    run(engine, _books(QUOTE + 4_000))
    assert _live(engine, "up")
    assert _live(engine, "down")


def test_late_window_and_fifteen_minute_clock(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open(), *_books(QUOTE)])
    run(engine, _books(T0 + 269_000))
    assert _live(engine, "up") and _live(engine, "down")
    run(engine, _books(T0 + 270_000))
    assert _live(engine) == []
    assert engine.state.markets["m1"].skip_counts.get("late_balanced", 0) >= 1
    engine.state.markets["m1"].bought_down_shares = Decimal("4")
    engine.state.markets["m1"].bought_down_cost = Decimal("1.92")
    run(engine, _books(T0 + 271_000))
    assert _live(engine, "down") == []
    assert _live(engine, "up")
    longer = _engine(tmp_path / "fifteen")
    run(longer, [_open(window=900), *_books(T0 + 839_000)])
    assert _live(longer)
    run(longer, _books(T0 + 840_000))
    assert _live(longer) == []
    assert longer.state.markets["m1"].skip_counts.get("late_balanced", 0) >= 1


def test_round_budget_stops_new_bids(tmp_path):
    engine = _engine(tmp_path, round_budget=Decimal("1.50"))
    run(engine, [_open(), *_books(QUOTE)])
    live = _live(engine)
    assert len(live) == 1
    spent = sum((order.size * order.price for order in live), Decimal("0"))
    assert spent <= Decimal("1.50")
    full = _engine(tmp_path / "spent")
    run(full, [_open(), *_books(QUOTE)])
    market = full.state.markets["m1"]
    market.bought_up_cost = Decimal("25")
    market.bought_up_shares = Decimal("40")
    run(full, _books(QUOTE + 2_000))
    assert _live(full) == []
    assert market.skip_counts.get("budget", 0) >= 1


def test_queue_fill_and_pnl_split(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open(), *_books(QUOTE, bid="0.47", ask="0.53", bid_size="1")])
    top = [
        order
        for order in _live(engine, "up")
        if order.price == Decimal("0.48")
    ]
    assert top
    assert top[0].size == Decimal("3")
    assert top[0].queue_ahead == 0
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "sell-up",
                "ts": QUOTE,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.48",
                "size": "3",
                "taker_side": "SELL",
                "trade_id": "sell-up",
            },
            {
                "type": "trade",
                "event_id": "sell-down",
                "ts": QUOTE,
                "market_id": "m1",
                "outcome": "down",
                "price": "0.48",
                "size": "3",
                "taker_side": "SELL",
                "trade_id": "sell-down",
            },
        ],
    )
    market = engine.state.markets["m1"]
    assert market.bought_up_shares == Decimal("3")
    assert market.bought_down_shares == Decimal("3")
    assert market.buy_up_fills == 1
    row = engine.summary()["rounds"][0]
    assert row["winner"] is None or row["winner"] == ""
    assert Decimal(row["pair_cost"]) == Decimal("0.96")
    assert Decimal(row["matched_shares"]) == Decimal("3")
    assert Decimal(row["pnl_matched"]) == Decimal("0.12")
    assert row["pnl"] is None
    assert row["excess_side"] == ""
    _redeem(engine)
    summary = engine.summary()
    row = summary["rounds"][0]
    assert row["winner"] == "up"
    assert Decimal(row["pnl"]) == Decimal("0.12")
    assert Decimal(row["pnl_matched"]) == Decimal("0.12")
    assert Decimal(row["pnl_excess"]) == Decimal("0")
    assert summary["strategy"] == "small_pair"
    assert Decimal(summary["round_totals"]["pnl"]) == Decimal("0.12")
    assert Decimal(summary["round_totals"]["pnl_matched"]) == Decimal("0.12")
    assert "dual_bid" not in json.dumps(summary["strategy"])


def test_excess_pnl_is_the_leftover(tmp_path):
    engine = _engine(tmp_path)
    run(engine, [_open(), *_books(QUOTE, bid="0.47", ask="0.53", bid_size="1")])
    run(
        engine,
        [
            {
                "type": "trade",
                "event_id": "sell-up",
                "ts": QUOTE,
                "market_id": "m1",
                "outcome": "up",
                "price": "0.48",
                "size": "3",
                "taker_side": "SELL",
                "trade_id": "sell-up",
            }
        ],
    )
    _redeem(engine)
    row = engine.summary()["rounds"][0]
    assert row["excess_side"] == "up"
    assert Decimal(row["excess_shares"]) == Decimal("3")
    assert Decimal(row["excess_price"]) == Decimal("0.48")
    assert Decimal(row["pnl_matched"]) == Decimal("0")
    assert Decimal(row["pnl_excess"]) == Decimal("1.56")
    assert Decimal(row["pnl"]) == Decimal("1.56")


def test_discovers_five_and_fifteen_minute_rounds(tmp_path):
    now = 1_700_000_000
    five = sessions_due("btc-updown-5m", now, 300, include_current=True)
    fifteen = sessions_due("btc-updown-15m", now, 300, include_current=True)
    assert five[0] == "btc-updown-5m-1699999800"
    assert fifteen[0] == "btc-updown-15m-1699999200"
    urls = []

    class _Rounds:
        def get_json(self, url: str):
            urls.append(url)
            return ROUNDS

    clock = {"t": float(now)}
    summary = run_paper(
        Config(
            strategy="small_pair",
            split_lead_seconds=300,
            poll_interval_seconds=2,
            max_open_markets=4,
            stale_after_ms=10_000,
        ),
        tmp_path / "paper",
        duration_seconds=2,
        max_markets=8,
        client=PublicReadClient(_Rounds(), network="mainnet", api_key="not-a-real-key"),
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    assert any("btc-updown-5m" in url and "/rounds" in url for url in urls)
    assert any("btc-updown-15m" in url and "/rounds" in url for url in urls)
    assert "not-a-real-key" not in json.dumps(summary)
    assert any("small_pair quotes from +2s" in note for note in summary["notes"])
    assert "btc-updown-5m-1699999800" in json.dumps(summary["notes"])


def test_json_capital_survives_for_the_buy_strategy(tmp_path, monkeypatch):
    derived = derive_from_split(Decimal("25"))
    assert derived.capital == Decimal("75")
    floor = derived.capital * (1 - derived.daily_fraction)
    assert floor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal("68.92")
    raw = json.loads((ROOT / "config.paper.json").read_text(encoding="utf-8"))
    raw["initial_capital"] = "5000"
    raw["max_loss_fraction"] = "0.20"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    for key in list(os.environ):
        if key.startswith("BOT_"):
            monkeypatch.delenv(key, raising=False)
    ladder = prepare_run(path, {}, max_markets=1)
    assert ladder.config.initial_capital == Decimal("75")
    args = Namespace(
        config=path,
        strategy=None,
        capital=None,
        max_loss_fraction=None,
        max_markets=None,
    )
    kept = _apply_strategy(_load(args), args)
    assert kept.strategy == "ladder"
    assert kept.initial_capital == Decimal("75")
    monkeypatch.setenv("BOT_MAX_MARKETS", "2")
    args.strategy = "small_pair"
    args.max_markets = 300
    bought = _apply_strategy(_load(args), args)
    assert bought.initial_capital == Decimal("5000")
    assert bought.max_loss_fraction == Decimal("0.20")
    assert bought.loss_floor() == Decimal("4000")
    assert args.max_markets == 300
    assert bought.max_open_markets >= 4
    monkeypatch.setenv("BOT_CAPITAL", "1234")
    from_env = _apply_strategy(_load(args), args)
    assert from_env.initial_capital == Decimal("1234")
    args.capital = "5000"
    args.max_loss_fraction = "0.20"
    flagged = _apply_strategy(_load(args), args)
    assert flagged.initial_capital == Decimal("5000")
    assert flagged.max_loss_fraction == Decimal("0.20")


def test_long_run_flags_and_live_refusal(tmp_path, monkeypatch, capsys):
    raw = json.loads((ROOT / "config.paper.json").read_text(encoding="utf-8"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    seen = {}

    def fake_run(config, out, duration_seconds, max_markets, env):
        seen["duration"] = duration_seconds
        seen["markets"] = max_markets
        seen["capital"] = config.initial_capital
        seen["fraction"] = config.max_loss_fraction
        return {
            "initial_capital": str(config.initial_capital),
            "loss_floor": str(config.loss_floor()),
            "markets": [],
        }

    monkeypatch.setattr("paper_split_maker.cli.run_paper", fake_run)
    for key in list(os.environ):
        if key.startswith("BOT_"):
            monkeypatch.delenv(key, raising=False)
    code = main(
        [
            "--config",
            str(path),
            "--out",
            str(tmp_path / "out"),
            "--strategy",
            "small_pair",
            "--max-markets",
            "300",
            "--duration-seconds",
            "90000",
            "--capital",
            "5000",
            "--max-loss-fraction",
            "0.20",
        ]
    )
    assert code == 0
    assert seen["duration"] == 90000
    assert seen["markets"] == 300
    assert seen["capital"] == Decimal("5000")
    assert seen["fraction"] == Decimal("0.20")
    refused = main(
        [
            "--config",
            str(path),
            "--out",
            str(tmp_path / "live"),
            "--strategy",
            "small_pair",
            "--live",
        ]
    )
    assert refused == 2
    assert "paper-only" in capsys.readouterr().out
    assert "not-a-real-key" not in capsys.readouterr().out
