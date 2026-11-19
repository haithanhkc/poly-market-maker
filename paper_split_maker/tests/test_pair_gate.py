"""Combined small_pair keeps both sides, but not a naked ladder."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paper_split_maker.cli import _apply_strategy, _load
from paper_split_maker.pair_replay import replay_combined_tape
from paper_split_maker.small_pair import plan_small_pair
from tests.helpers import make_engine, run
from tests.test_small_pair import OPEN, T0, _books, _open

ROOT = Path(__file__).resolve().parents[1]


def _quote(**overrides):
    values = dict(
        up_bid=Decimal("0.36"),
        up_ask=Decimal("0.40"),
        up_levels=[(Decimal("0.36"), Decimal("10"))],
        down_bid=Decimal("0.60"),
        down_ask=Decimal("0.64"),
        down_levels=[(Decimal("0.60"), Decimal("10"))],
        tick=Decimal("0.01"),
        up_shares=Decimal("0"),
        down_shares=Decimal("0"),
        budget_left=Decimal("25"),
        age_s=40,
        window_s=300,
        fair_up=Decimal("0.38"),
        binance_filter=True,
        binance_age_s=0,
        fair_quote=True,
        start_s=10,
    )
    values.update(overrides)
    return plan_small_pair(**values)


def _sizes(plan, side):
    return [bid.size for bid in plan.bids if bid.outcome == side]


def test_pair_gate_rests_one_lot_and_counts_a_live_ladder():
    open_book = _quote(pair_gate=True)
    assert _sizes(open_book, "up")
    assert _sizes(open_book, "down")
    assert len(_sizes(open_book, "up")) == 1
    assert len(_sizes(open_book, "down")) == 1
    naked = _quote(pair_gate=False)
    assert sum(_sizes(naked, "down"), Decimal("0")) > sum(_sizes(open_book, "down"), Decimal("0"))
    # Twelve live shares are a potential fill. The replacement book is one lot.
    capped = _quote(pair_gate=True, resting_down=Decimal("12"))
    assert sum(_sizes(capped, "down"), Decimal("0")) <= Decimal("8")
    assert len(_sizes(capped, "down")) == 1
    filled = _quote(pair_gate=True, down_shares=Decimal("2"), down_cost=Decimal("1.20"))
    assert _sizes(filled, "down") == []
    assert _sizes(filled, "up")


def test_catch_up_lifts_the_lagging_bid_without_crossing():
    plan = _quote(
        pair_gate=True,
        catch_up=True,
        down_shares=Decimal("2"),
        down_cost=Decimal("1.20"),
        up_bid=Decimal("0.30"),
        up_ask=Decimal("0.40"),
    )
    ups = [bid for bid in plan.bids if bid.outcome == "up"]
    assert ups
    assert ups[0].price < Decimal("0.40")
    assert Decimal("1.20") / Decimal("2") + ups[0].price <= Decimal("0.97")
    assert ups[0].price > Decimal("0.30")
    assert plan.action == "catch_up"


def test_late_round_does_not_open_a_new_pair():
    flat = _quote(pair_gate=True, first_leg_s=200, age_s=200)
    assert flat.bids == []
    assert flat.action == "first_leg"
    leg = _quote(
        pair_gate=True,
        catch_up=True,
        first_leg_s=200,
        age_s=210,
        down_shares=Decimal("2"),
        down_cost=Decimal("1.20"),
    )
    assert _sizes(leg, "down") == []
    assert _sizes(leg, "up")
    early = _quote(pair_gate=True, first_leg_s=200, age_s=199)
    assert _sizes(early, "up") and _sizes(early, "down")


def test_binance_mode_keeps_the_twelve_share_favorite_cap():
    favorite = _quote(
        fair_quote=False,
        pair_gate=False,
        fair_up=Decimal("0.70"),
        up_bid=Decimal("0.68"),
        up_ask=Decimal("0.72"),
        up_shares=Decimal("11"),
    )
    assert _sizes(favorite, "up")


def test_replay_cuts_the_naked_leg():
    before = replay_combined_tape(False)
    after = replay_combined_tape(True)
    assert before["one_sided"] == 13
    assert before["adverse_unpaired"] == Decimal("16")
    assert after["adverse_unpaired"] <= Decimal("3")
    assert after["adverse_excess"] > before["adverse_excess"]
    assert after["max_unpaired"] < before["max_unpaired"]
    assert after["pnl_excess"] > before["pnl_excess"]
    assert after["avg_pair_cost"] <= Decimal("0.97")
    # The one-lot cap gives back most of the naked-leg loss on the 13 adverse tapes.
    saved = after["adverse_excess"] - before["adverse_excess"]
    assert saved > Decimal("100")


def test_combined_engine_posts_one_rung_per_side(tmp_path):
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
    quote = T0 + 10_000
    run(engine, [_open()])
    run(engine, _books(quote))
    live = [
        order
        for order in engine.state.orders.values()
        if order.side == "buy" and order.status in {"queued", "live"}
    ]
    assert len([order for order in live if order.outcome == "up"]) == 1
    assert len([order for order in live if order.outcome == "down"]) == 1


def test_cli_flags_override_the_combined_defaults(tmp_path):
    raw = (ROOT / "config.paper.json").read_text(encoding="utf-8")
    path = tmp_path / "config.json"
    path.write_text(raw, encoding="utf-8")
    from argparse import Namespace

    args = Namespace(
        config=path,
        max_markets=1,
        strategy="small_pair",
        series=None,
        capital=None,
        max_loss_fraction=None,
        binance_lean=True,
        price_source="combined",
        pair_gate=False,
        catch_up=False,
        first_leg_seconds=0,
    )
    loaded = _apply_strategy(_load(args), args)
    assert loaded.price_source == "combined"
    assert loaded.small_pair_pair_gate is False
    assert loaded.small_pair_catch_up is False
    assert loaded.small_pair_first_leg_seconds == 0
