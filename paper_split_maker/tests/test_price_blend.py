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
        basis=None,
    )
    values.update(overrides)
    return combine_prices(**values)


def test_structural_basis_is_removed_and_the_binance_move_is_kept():
    steady = _view(
        binance=Decimal("100120"),
        binance_at_chainlink=Decimal("100120"),
        basis=Decimal("120"),
    )
    assert steady.spot == Decimal("100120")
    assert steady.residual == Decimal("0")
    assert steady.disagree is False
    assert steady.widen is False
    moved = _view(basis=Decimal("10"))
    assert moved.spot == Decimal("100040")
    assert moved.residual == Decimal("0")
    assert moved.widen is False


def test_only_a_real_residual_or_a_stale_feed_widens():
    rich = _view(
        binance=Decimal("101200"),
        binance_at_chainlink=Decimal("101200"),
        basis=Decimal("10"),
    )
    assert rich.residual == Decimal("1190")
    assert rich.disagree is True
    assert rich.widen is True
    stale = _view(binance_age_s=3, basis=Decimal("10"))
    assert stale.disagree is False
    assert stale.widen is True
    missing = _view(chainlink=None, binance_at_chainlink=None)
    assert missing.spot == Decimal("100040")
    assert missing.spot_source == "binance"
    assert missing.widen is True


def test_stale_chainlink_does_not_replace_binance():
    """Field print: on-chain 60186.846 at 50s, Binance 60334, predict S0 60324.875."""
    from paper_split_maker.binance_btc import fair_up

    stale = _view(
        binance=Decimal("60334"),
        chainlink=Decimal("60186.846"),
        binance_at_chainlink=Decimal("60334"),
        basis=Decimal("10"),
        chainlink_age_s=50,
        binance_age_s=0.2,
    )
    assert stale.spot == Decimal("60334")
    assert stale.spot_source == "binance"
    assert stale.widen is False
    missing_anchor = _view(
        binance=Decimal("60334"),
        chainlink=Decimal("60186.846"),
        binance_at_chainlink=None,
        basis=None,
        chainlink_age_s=10,
    )
    assert missing_anchor.spot == Decimal("60334")
    assert missing_anchor.spot_source == "binance"
    assert missing_anchor.widen is False
    dead = _view(
        binance=Decimal("60334"),
        chainlink=Decimal("60186.846"),
        binance_at_chainlink=None,
        chainlink_age_s=4000,
    )
    assert dead.spot == Decimal("60334")
    assert dead.widen is True
    fair = fair_up(Decimal("60334"), Decimal("60324.875"), None, 200, 0.0001)
    assert Decimal("0.40") < fair < Decimal("0.70")


def test_fair_targets_quote_both_sides_under_the_pair_sum():
    from paper_split_maker.price_blend import fair_side_targets
    from paper_split_maker.small_pair import plan_small_pair

    up, down = fair_side_targets(Decimal("0.62"), Decimal("0.96"), Decimal("0.01"))
    assert up > Decimal("0.5")
    assert down < Decimal("0.5")
    assert up + down <= Decimal("0.96")
    assert up <= Decimal("0.62")
    assert down <= Decimal("0.38")
    plan = plan_small_pair(
        up_bid=Decimal("0.55"),
        up_ask=Decimal("0.70"),
        up_levels=[],
        down_bid=Decimal("0.45"),
        down_ask=Decimal("0.70"),
        down_levels=[],
        tick=Decimal("0.01"),
        up_shares=Decimal("0"),
        down_shares=Decimal("0"),
        budget_left=Decimal("25"),
        age_s=40,
        window_s=300,
        fair_up=Decimal("0.62"),
        binance_filter=True,
        binance_age_s=0,
        fair_quote=True,
        neutral_band=Decimal("0.10"),
    )
    sides = {bid.outcome for bid in plan.bids}
    assert sides == {"up", "down"}
    tops = {}
    for bid in plan.bids:
        tops[bid.outcome] = max(tops.get(bid.outcome, Decimal("0")), bid.price)
    assert tops["up"] + tops["down"] <= Decimal("0.96")
    assert tops["down"] <= Decimal("0.38")
    assert tops["down"] < Decimal("0.45")
    assert plan.action == "fair_quote"
    down_count = sum(1 for bid in plan.bids if bid.outcome == "down")
    up_count = sum(1 for bid in plan.bids if bid.outcome == "up")
    assert down_count >= 1
    assert down_count < up_count


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
    assert market.last_combined == Decimal("100040")
    assert market.last_basis == Decimal("10")
    assert market.last_residual == Decimal("0")
    assert market.last_fair_up is not None
    assert market.price_widen is False
    assert market.last_up_target is not None
    assert market.last_down_target is not None
    text = capsys.readouterr().err
    assert "binance=100040" in text
    assert "chainlink=100000" in text
    assert "combined=100040" in text
    assert "up_target=" in text
    assert "down_target=" in text
    assert "up_best=" in text
    row = round_report(market)
    assert Decimal(row["combined_price"]) == Decimal("100040")
    assert Decimal(row["residual"]) == Decimal("0")


def test_stale_chainlink_still_bids_both_sides(tmp_path, capsys):
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
        def __init__(self):
            self.snap = None

        def latest(self):
            return (engine.state.now_ms, Decimal("60334"))

        def price_at(self, ts_ms):
            return self.snap

        def vol(self, now_ms):
            return None

    class Link:
        def latest(self):
            return (engine.state.now_ms - 50_000, Decimal("60186.846"))

        def price_at(self, ts_ms):
            return Decimal("60186.846")

    binance = Binance()
    engine.binance_feed = binance
    engine.chainlink_feed = Link()
    quote = T0 + 50_000
    run(
        engine,
        [
            market_event(
                slug=f"btc-updown-5m-{OPEN}",
                end_ts=(OPEN + 300) * 1000,
                ts=T0,
                reference_price="60324.875",
            ),
            book_event("m1", "up", "0.54", "0.57", ts=quote, bid_size="20", digest="up"),
            book_event("m1", "down", "0.43", "0.46", ts=quote, bid_size="20", digest="down"),
            {"type": "heartbeat", "event_id": "hb-stale-link", "ts": quote, "market_id": "m1"},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.reference_source == "predict"
    assert market.reference_price == Decimal("60324.875")
    assert market.last_combined == Decimal("60334")
    assert market.price_spot_source == "binance"
    assert market.last_fair_up is not None and market.last_fair_up > Decimal("0.40")
    assert market.last_up_target is not None and market.last_up_target > Decimal("0.20")
    assert "stale_feed" not in (market.skip_counts or {})
    sides = {order.outcome for order in engine.state.orders.values()}
    assert sides == {"up", "down"}
    binance.snap = Decimal("60334")
    run(
        engine,
        [{"type": "heartbeat", "event_id": "hb-anchor", "ts": quote + 1000, "market_id": "m1"}],
    )
    assert engine._chainlink_anchor_px == Decimal("60334")
    assert market.last_combined == Decimal("60334")
    text = capsys.readouterr().err
    assert "chainlink=60186.846" in text
    assert "binance=60334" in text


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
