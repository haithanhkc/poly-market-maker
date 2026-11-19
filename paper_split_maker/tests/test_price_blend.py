"""Chainlink blend. No network and no orders."""

from __future__ import annotations

import json
from argparse import Namespace
from decimal import Decimal

from paper_split_maker.chainlink_btc import decode_latest_round
from paper_split_maker.cli import _apply_strategy, _load, main
from paper_split_maker.config import ConfigError, config_from_mapping
from paper_split_maker.price_blend import combine_prices
from paper_split_maker.small_pair import round_report
from tests.helpers import book_event, make_engine, market_event, run

OPEN = 1_700_000_000
T0 = OPEN * 1000
ROOT_CONFIG = __import__("pathlib").Path(__file__).resolve().parents[1] / "config.paper.json"


def _view(**overrides):
    values = dict(
        mode="combined",
        binance=Decimal("100040"),
        chainlink=Decimal("100000"),
        binance_at_chainlink=Decimal("100010"),
        binance_age_s=0.2,
        chainlink_age_s=12,
        binance_stale_s=2,
        chainlink_stale_s=3600,
        disagree_fraction=Decimal("0.001"),
    )
    values.update(overrides)
    return combine_prices(**values)


def test_combined_spot_is_the_chainlink_level_plus_the_binance_move():
    view = _view()
    assert view.spot == Decimal("100030")
    assert view.spot_source == "combined"
    assert view.price_gap == Decimal("40")
    assert view.disagree is False
    assert view.widen is False


def test_disagreement_or_a_stale_feed_widens():
    rich = _view(binance=Decimal("101200"))
    assert rich.disagree is True
    assert rich.widen is True
    stale = _view(binance_age_s=3)
    assert stale.disagree is False
    assert stale.widen is True
    missing = _view(chainlink=None, binance_at_chainlink=None)
    assert missing.spot == Decimal("100040")
    assert missing.spot_source == "binance"
    assert missing.widen is True


def test_binance_mode_ignores_chainlink():
    view = _view(mode="binance", binance_age_s=9, chainlink=None)
    assert view.spot == Decimal("100040")
    assert view.widen is False
    only = _view(mode="chainlink", binance=None, binance_at_chainlink=None)
    assert only.spot == Decimal("100000")
    assert only.widen is False


def test_latest_round_data_uses_eight_decimals_and_updated_at():
    answer = (100_000 * 10**8).to_bytes(32, "big")
    updated = (1_700_000_100).to_bytes(32, "big")
    payload = "0x" + (
        (7).to_bytes(32, "big") + answer + (1_700_000_000).to_bytes(32, "big") + updated + (7).to_bytes(32, "big")
    ).hex()
    stamp, price = decode_latest_round(payload)
    assert stamp == 1_700_000_100_000
    assert price == Decimal("100000")
    assert decode_latest_round("0x01") is None


def test_bad_price_source_is_rejected():
    raw = json.loads(ROOT_CONFIG.read_text(encoding="utf-8"))
    raw["price_source"] = "mystery"
    try:
        config_from_mapping(raw)
    except ConfigError as exc:
        assert "price_source" in str(exc)
    else:
        raise AssertionError("mystery source was accepted")


def test_engine_logs_both_prices_and_the_combined_spot(tmp_path, capsys):
    engine = make_engine(
        tmp_path,
        strategy="small_pair",
        round_budget=Decimal("25"),
        per_market_budget=Decimal("25"),
        stale_after_ms=500_000,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        small_pair_binance=True,
        price_source="combined",
        initial_capital=Decimal("100"),
        max_loss_fraction=Decimal("0.99"),
    )

    class Binance:
        def latest(self):
            return (engine.state.now_ms, Decimal("100040"))

        def price_at(self, ts_ms):
            return Decimal("100010")

        def vol(self, now_ms):
            return 0.0002

    class Link:
        def latest(self):
            return (engine.state.now_ms - 1000, Decimal("100000"))

        def price_at(self, ts_ms):
            return Decimal("100000")

    engine.binance_feed = Binance()
    engine.chainlink_feed = Link()
    quote = T0 + 10_000
    run(
        engine,
        [
            market_event(
                slug=f"btc-updown-5m-{OPEN}",
                end_ts=(OPEN + 300) * 1000,
                ts=T0,
                reference_price="100000",
            )
        ],
    )
    run(
        engine,
        [
            book_event("m1", "up", "0.48", "0.52", ts=quote, bid_size="1", digest="up"),
            book_event("m1", "down", "0.48", "0.52", ts=quote, bid_size="1", digest="down"),
            {"type": "heartbeat", "event_id": "hb", "ts": quote, "market_id": "m1"},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.reference_source == "predict"
    assert market.reference_price == Decimal("100000")
    assert market.last_combined == Decimal("100030")
    assert market.last_fair_up is not None
    assert market.price_widen is False
    text = capsys.readouterr().err
    assert "binance=100040" in text
    assert "chainlink=100000" in text
    assert "price_gap=40" in text
    assert "combined=100030" in text
    assert "fair_up=" in text
    row = round_report(market)
    assert Decimal(row["combined_price"]) == Decimal("100030")
    assert Decimal(row["price_gap"]) == Decimal("40")


def test_cli_price_source_defaults_to_binance(tmp_path, monkeypatch):
    raw = json.loads(ROOT_CONFIG.read_text(encoding="utf-8"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    seen = {}

    def fake_run(config, out, duration_seconds, max_markets, env):
        seen["source"] = config.price_source
        return {"markets": []}

    monkeypatch.setattr("paper_split_maker.cli.run_paper", fake_run)
    code = main(
        [
            "--config",
            str(path),
            "--out",
            str(tmp_path / "out"),
            "--strategy",
            "small_pair",
            "--binance-lean",
            "--price-source",
            "combined",
            "--duration-seconds",
            "1",
            "--max-markets",
            "1",
        ]
    )
    assert code == 0
    assert seen["source"] == "combined"
    args = Namespace(
        config=path,
        max_markets=1,
        strategy="small_pair",
        series=None,
        capital=None,
        max_loss_fraction=None,
        binance_lean=True,
        price_source=None,
    )
    loaded = _apply_strategy(_load(args), args)
    assert loaded.price_source == "binance"
