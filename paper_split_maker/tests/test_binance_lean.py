"""Binance fair value and the small_pair lean. No network and no orders."""

from __future__ import annotations

import json
import math
import os
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import pytest

from paper_split_maker.binance_btc import (
    BinanceBtcFeed,
    PriceBuffer,
    decode_frame,
    fair_up,
    parse_price_message,
    parse_rest_ticker,
    realized_vol,
)
from paper_split_maker.cli import _apply_strategy, _load, main
from paper_split_maker.config import config_from_mapping, load_config
from paper_split_maker.normalize import gamma_market_event
from paper_split_maker.public_client import category_to_market
from paper_split_maker.session import run_paper
from paper_split_maker.small_pair import plan_small_pair, round_report
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


def _phi(spot, open_px, sigma, remaining):
    z = math.log(spot / open_px) / (sigma * math.sqrt(remaining))
    return Decimal(f"{0.5 * (1.0 + math.erf(z / math.sqrt(2.0))):.6f}")


def test_fair_up_matches_the_normal_cdf_and_the_vol_floor():
    assert fair_up(100, 100, 0.001, 100, Decimal("0.0001")) == Decimal("0.500000")
    above = fair_up(101, 100, 0.001, 100, Decimal("0.0001"))
    assert above == _phi(101, 100, 0.001, 100)
    assert above > Decimal("0.5")
    closer = fair_up(101, 100, 0.001, 10, Decimal("0.0001"))
    assert closer > above
    below = fair_up(99, 100, 0.001, 100, Decimal("0.0001"))
    assert below < Decimal("0.5")
    floored = fair_up(Decimal("100.01"), 100, 0, 100, Decimal("0.0001"))
    assert floored == _phi(100.01, 100, 0.0001, 100)
    wider = fair_up(Decimal("100.01"), 100, Decimal("0.01"), 100, Decimal("0.0001"))
    assert wider == _phi(100.01, 100, 0.01, 100)
    assert floored > wider
    assert fair_up(101, 100, 0.001, 0, Decimal("0.0001")) == fair_up(
        101, 100, 0.001, 1, Decimal("0.0001")
    )
    assert fair_up(0, 100, 0.001, 100, Decimal("0.0001")) is None


def test_realized_vol_is_zero_on_a_flat_tape_and_positive_after_a_move():
    flat = [(index * 1000, Decimal("64000")) for index in range(5)]
    assert realized_vol(flat, 4_000, 600) == 0.0
    moved = [
        (0, Decimal("64000")),
        (1000, Decimal("64100")),
        (2000, Decimal("64050")),
    ]
    sigma = realized_vol(moved, 2000, 600)
    assert sigma is not None and sigma > 0
    assert realized_vol(moved[:2], 2000, 600) is None
    buffer = PriceBuffer()
    for stamp, price in moved:
        buffer.add(stamp, price)
    assert buffer.price_at(1000) == Decimal("64100")
    assert buffer.price_at(-1) == Decimal("64000")
    assert buffer.latest()[1] == Decimal("64050")


def test_feed_parsers_and_an_unmasked_frame():
    trade = parse_price_message(
        {"e": "trade", "p": "64000.5", "T": 1_700_000_000_000}
    )
    assert trade == (1_700_000_000_000, Decimal("64000.5"))
    agg = parse_price_message({"e": "aggTrade", "p": "64001", "T": 15})
    assert agg == (15, Decimal("64001"))
    wrapped = parse_price_message(
        {"stream": "btcusdt@trade", "data": {"e": "trade", "p": "10", "T": 3}}
    )
    assert wrapped == (3, Decimal("10"))
    book = parse_price_message({"b": "100", "a": "102", "E": 9})
    assert book == (9, Decimal("101"))
    assert parse_price_message({"e": "trade"}) is None
    assert parse_price_message("not-json") is None
    rest = parse_rest_ticker({"symbol": "BTCUSDT", "price": "64000.50"}, 42)
    assert rest == (42, Decimal("64000.50"))
    assert parse_rest_ticker({"price": "0"}, 1) is None
    opcode, payload = decode_frame(bytes([0x81, 0x02]) + b"hi")
    assert opcode == 0x1
    assert payload == b"hi"
    feed = BinanceBtcFeed(
        fetch_rest=lambda url: {"symbol": "BTCUSDT", "price": "64000.50"},
        now_ms=lambda: 1000,
    )
    feed._rest_once()
    assert feed.rest_polls == 1
    assert feed.latest() == (1000, Decimal("64000.50"))
    assert feed.connected is False


def _plan(**overrides):
    values = dict(
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
        age_s=10,
        window_s=300,
    )
    values.update(overrides)
    return plan_small_pair(**values)


def _tops(plan):
    up = [bid.price for bid in plan.bids if bid.outcome == "up"]
    down = [bid.price for bid in plan.bids if bid.outcome == "down"]
    return (up[0] if up else None), (down[0] if down else None)


def test_lean_recentres_past_the_gap_and_keeps_the_pair_under_the_target():
    untouched = _plan(fair_up=Decimal("0.51"))
    assert _tops(untouched) == (Decimal("0.48"), Decimal("0.48"))
    assert untouched.action == "mid"
    leaned = _plan(
        fair_up=Decimal("0.70"),
        up_bid=Decimal("0.20"),
        up_ask=Decimal("0.80"),
        down_bid=Decimal("0.20"),
        down_ask=Decimal("0.80"),
    )
    up, down = _tops(leaned)
    assert up > Decimal("0.48")
    assert down < Decimal("0.48")
    assert up + down <= Decimal("0.96")
    assert leaned.action == "fair"
    tight = _plan(fair_up=Decimal("0.70"))
    up, down = _tops(tight)
    assert Decimal("0.48") < up < Decimal("0.52")
    assert up + down <= Decimal("0.96")


def test_pulls_the_likely_loser_and_stops_adding_to_a_heavy_loser():
    pulled = _plan(age_s=60, fair_up=Decimal("0.20"))
    sides = {bid.outcome for bid in pulled.bids}
    assert "up" not in sides
    assert "down" in sides
    assert pulled.action == "pull_loser"
    other = _plan(age_s=60, fair_up=Decimal("0.80"))
    sides = {bid.outcome for bid in other.bids}
    assert "down" not in sides
    assert "up" in sides
    early = _plan(age_s=5, fair_up=Decimal("0.20"))
    assert "up" in {bid.outcome for bid in early.bids}
    heavy = _plan(
        age_s=5,
        fair_up=Decimal("0.40"),
        up_shares=Decimal("2"),
        down_shares=Decimal("0"),
    )
    sides = {bid.outcome for bid in heavy.bids}
    assert "up" not in sides
    assert "down" in sides
    assert heavy.action == "heavy_loser"
    assert heavy.reason == "heavy_loser"


def test_predict_strike_is_the_reference_when_the_payload_has_one():
    category = json.loads(json.dumps(ROUNDS["data"][0]))
    category["markets"][0]["priceToBeat"] = "64123.5"
    converted = category_to_market(category, "btc-updown-5m")
    assert converted["referencePrice"] == "64123.5"
    event = gamma_market_event(converted)
    assert event["reference_price"] == "64123.5"
    bare = category_to_market(ROUNDS["data"][0], "btc-updown-5m")
    assert "referencePrice" not in bare


def test_engine_uses_binance_at_the_open_and_records_the_close(tmp_path, capsys):
    engine = make_engine(
        tmp_path,
        strategy="small_pair",
        round_budget=Decimal("25"),
        per_market_budget=Decimal("25"),
        stale_after_ms=500_000,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        small_pair_binance=True,
    )

    class Feed:
        def latest(self):
            return (engine.state.now_ms, Decimal("65000"))

        def price_at(self, ts_ms):
            return Decimal("64000")

        def vol(self, now_ms):
            return 0.0002

    engine.binance_feed = Feed()
    run(
        engine,
        [
            market_event(
                slug=f"btc-updown-5m-{OPEN}",
                end_ts=(OPEN + 300) * 1000,
                ts=T0,
            )
        ],
    )
    events = []
    for outcome in ("up", "down"):
        events.append(
            book_event(
                "m1",
                outcome,
                "0.48",
                "0.52",
                ts=QUOTE,
                bid_size="10",
                ask_size="10",
                digest=f"{outcome}-{QUOTE}",
            )
        )
    events.append({"type": "heartbeat", "event_id": "hb-q", "ts": QUOTE, "market_id": "m1"})
    run(engine, events)
    market = engine.state.markets["m1"]
    assert market.reference_source == "predict" or market.reference_source == "binance"
    assert market.reference_source == "binance"
    assert market.reference_price == Decimal("64000")
    assert market.last_fair_up > Decimal("0.5")
    assert market.fair_gap_n == 1
    text = capsys.readouterr().err
    assert "reference source=binance" in text
    assert "binance_lean" in text
    assert "fair_up=" in text
    close = QUOTE + 297_000
    for outcome in ("up", "down"):
        events = [
            book_event(
                "m1",
                outcome,
                "0.48",
                "0.52",
                ts=close,
                bid_size="10",
                ask_size="10",
                digest=f"close-{outcome}",
            )
        ]
        run(engine, events)
    run(engine, [{"type": "heartbeat", "event_id": "hb-close", "ts": close, "market_id": "m1"}])
    assert market.binance_close_locked is True
    assert market.binance_implied_winner == "up"
    market.winner = "down"
    row = round_report(market)
    assert row["winner_vs_binance"] == "disagree"
    assert row["reference_source"] == "binance"
    assert Decimal(row["fair_gap_max"]) >= Decimal("0")
    assert row["fair_gap_mean"] is not None
    market.winner = "up"
    assert round_report(market)["winner_vs_binance"] == "agree"


def test_binance_flag_starts_and_stops_a_fake_feed(tmp_path, monkeypatch):
    created = []

    class FakeFeed:
        def __init__(self, lookback_s=600):
            created.append(self)
            self.lookback_s = lookback_s

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def latest(self):
            return None

        def price_at(self, ts_ms):
            return None

        def vol(self, now_ms):
            return None

    monkeypatch.setattr("paper_split_maker.binance_btc.BinanceBtcFeed", FakeFeed)
    raw = json.loads((ROOT / "config.paper.json").read_text(encoding="utf-8"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    for key in list(os.environ):
        if key.startswith("BOT_"):
            monkeypatch.delenv(key, raising=False)
    args = Namespace(
        config=path,
        strategy="small_pair",
        series="btc-updown-5m",
        capital="5000",
        max_loss_fraction="0.20",
        max_markets=1,
        binance_lean=False,
    )
    quiet = _apply_strategy(_load(args), args)
    assert quiet.small_pair_binance is False

    class _Rounds:
        def get_json(self, url: str):
            return ROUNDS

    from paper_split_maker.public_client import PublicReadClient

    clock = {"t": 1_700_000_000.0}
    run_paper(
        quiet,
        tmp_path / "off",
        duration_seconds=1,
        max_markets=1,
        client=PublicReadClient(_Rounds(), network="mainnet", api_key="not-a-real-key"),
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    assert created == []
    args.binance_lean = True
    leaned = _apply_strategy(_load(args), args)
    assert leaned.small_pair_binance is True
    clock["t"] = 1_700_000_000.0
    run_paper(
        leaned,
        tmp_path / "on",
        duration_seconds=1,
        max_markets=1,
        client=PublicReadClient(_Rounds(), network="mainnet", api_key="not-a-real-key"),
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    assert len(created) == 1
    assert created[0].started is True
    assert created[0].stopped is True
    seen = {}

    def fake_run(config, out, duration_seconds, max_markets, env):
        seen["lean"] = config.small_pair_binance
        return {"markets": []}

    monkeypatch.setattr("paper_split_maker.cli.run_paper", fake_run)
    code = main(
        [
            "--config",
            str(path),
            "--out",
            str(tmp_path / "cli"),
            "--strategy",
            "small_pair",
            "--binance-lean",
            "--capital",
            "5000",
        ]
    )
    assert code == 0
    assert seen["lean"] is True
    raw["small_pair_binance"] = True
    assert config_from_mapping(raw).small_pair_binance is True
    with pytest.raises(Exception):
        bad = dict(raw)
        bad["small_pair_sigma_floor"] = "0"
        config_from_mapping(bad)
