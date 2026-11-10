"""Offline checks for the reconstructed historical replay."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from paper_split_maker.config import Config, ConfigError
from paper_split_maker.historical import (
    LABEL,
    build_replay_events,
    load_historical_snapshot,
    reconstruct_top,
    run_historical,
    window_starts,
)
ROOT = Path(__file__).resolve().parents[1]


def _capital_200() -> Config:
    """The old 200 USDC replay book. It is not a run config."""
    return Config(
        initial_capital=Decimal("200"),
        per_market_budget=Decimal("8"),
        split_clip=Decimal("5"),
        min_pair_inventory=Decimal("5"),
    )


START = 1_700_000_000


def _gamma(slug, condition, end_s, up="111", down="222"):
    return {
        "id": slug,
        "version": "v1",
        "conditionId": condition,
        "slug": slug,
        "question": slug,
        "outcomes": ["Up", "Down"],
        "clobTokenIds": [up, down],
        "endDate": end_s,
        "eventStartTime": START,
        "closed": True,
        "acceptingOrders": False,
        "orderPriceMinTickSize": "0.001",
        "orderMinSize": "5",
        "umaResolutionStatus": "resolved",
    }


def _print(ts, side, outcome, price, size, suffix):
    return {
        "ts": ts,
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": size,
        "tx": suffix,
        "id": suffix,
    }


def _snapshot():
    first_end = START + 40
    second = START + 200
    return {
        "label": LABEL,
        "synthetic_order_books": True,
        "markets": [
            {
                "slug": "m1",
                "window_start": START,
                "window_end": first_end,
                "window_start_bkk": "test",
                "window_end_bkk": "test",
                "gamma": _gamma("m1", "0x1", first_end),
                "prints": [
                    _print(START, "BUY", "up", "0.55", "10", "a"),
                    _print(START, "SELL", "up", "0.54", "10", "b"),
                    _print(START, "BUY", "down", "0.46", "10", "c"),
                    _print(START, "SELL", "down", "0.44", "10", "d"),
                    _print(START + 2, "BUY", "up", "0.55", "7", "e1"),
                    _print(START + 2, "BUY", "up", "0.56", "20", "e2"),
                    _print(START + 2, "SELL", "up", "0.54", "100", "e3"),
                ],
                "seed_prints": [],
                "price_history": {
                    "up": [
                        {"timestamp": START, "price": "0.545", "resolution_seconds": 60}
                    ],
                    "down": [
                        {"timestamp": START, "price": "0.45", "resolution_seconds": 60}
                    ],
                },
                "resolution": {
                    "data": [
                        {
                            "condition_id": "0x1",
                            "status": "resolved",
                            "payouts": [1_000_000, 0],
                        }
                    ]
                },
                "print_count": 8,
            },
            {
                "slug": "m2",
                "window_start": second,
                "window_end": second + 40,
                "window_start_bkk": "test",
                "window_end_bkk": "test",
                "gamma": _gamma("m2", "0x2", second + 40, up="333", down="444"),
                "prints": [],
                "seed_prints": [],
                "price_history": {"up": [], "down": []},
                "resolution": {
                    "data": [
                        {
                            "condition_id": "0x2",
                            "status": "resolved",
                            "payouts": [0, 1_000_000],
                        }
                    ]
                },
                "print_count": 0,
            },
        ],
    }


def test_window_starts_are_ten_spread_slots():
    start = 1793984400
    end = start + 86400
    starts = window_starts(start, end)
    assert len(starts) == 10
    assert starts[0] == start
    assert starts[-1] < end
    assert all(item % 300 == 0 for item in starts)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 300


def test_reconstructed_top_widens_a_tight_print_and_rejects_disagreement():
    tick = Decimal("0.001")
    top = reconstruct_top(
        (Decimal("0.55"), Decimal("10")),
        (Decimal("0.54"), Decimal("10")),
        tick,
        Decimal("0.545"),
    )
    assert top is not None
    assert top["ask"] == Decimal("0.55")
    assert top["bid"] == Decimal("0.53")
    assert top["widened"] is True
    assert top["ask_size"] == Decimal("5")
    assert (
        reconstruct_top(
            (Decimal("0.55"), Decimal("10")),
            (Decimal("0.54"), Decimal("10")),
            tick,
            Decimal("0.10"),
        )
        is None
    )


def test_historical_replay_queue_fill_floor_and_decline(tmp_path: Path):
    config = _capital_200()
    assert config.loss_floor() == Decimal("194")
    assert config.initial_capital == Decimal("200")
    summary = run_historical(config, _snapshot(), tmp_path)
    assert Decimal(summary["loss_floor"]) == Decimal("194")
    report = json_report(tmp_path)
    assert report["not_evidence_of_live_profitability"] is True
    assert report["counterfactual"] is True
    by_slug = {row["slug"]: row for row in report["markets"]}
    assert by_slug["m1"]["fills_up"] == "2"
    # The other leg is 2 shares, under the minimum, and the bid is below 1 - P.
    assert by_slug["m1"]["fills_down"] == "0"
    assert by_slug["m1"]["splits"] == "5"
    assert by_slug["m1"]["declined_to_trade"] is False
    assert Decimal(by_slug["m1"]["realized_pnl"]) == Decimal("-0.9")
    assert by_slug["m2"]["declined_to_trade"] is True
    assert by_slug["m2"]["splits"] == "0"
    assert report["markets_declined"] == 1
    assert Decimal(report["net_pnl_usdc"]) == Decimal("-0.9")
    assert Decimal(report["worst_case_equity_minimum"]) >= Decimal("194")
    assert Decimal(report["rebate_estimate_unguaranteed"]) > 0
    assert report["rebate_included_in_pnl"] is False


def test_unlabeled_snapshot_is_refused(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text('{"label": "public", "markets": []}', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_historical_snapshot(path)
    snapshot = _snapshot()
    snapshot["label"] = "official-looking"
    with pytest.raises(ConfigError):
        build_replay_events(snapshot)


def test_historical_replay_writes_the_csv(tmp_path: Path):
    snapshot = tmp_path / "snap.json"
    import json

    snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")
    out = tmp_path / "out"
    run_historical(_capital_200(), load_historical_snapshot(snapshot), out)
    assert (out / "historical_results.csv").exists()


def json_report(out: Path) -> dict:
    import json

    return json.loads((out / "historical_results.json").read_text(encoding="utf-8"))
