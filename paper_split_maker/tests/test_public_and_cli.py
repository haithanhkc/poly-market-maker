"""Parsers, the bounded paper loop, and the offline CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_split_maker.config import Config, ConfigError, load_config
from paper_split_maker.normalize import (
    data_api_resolution_event,
    data_api_trade_events,
    events_from_ws_message,
    gamma_market_event,
)
from paper_split_maker.public_client import (
    GAMMA,
    PublicDataError,
    PublicReadClient,
    UrllibTransport,
)
from paper_split_maker.session import run_demo, run_paper, run_replay
from tests.helpers import make_engine

ROOT = Path(__file__).resolve().parents[1]


def _gamma(**overrides):
    payload = {
        "id": "11",
        "version": "v1",
        "conditionId": "0xabc",
        "slug": "btc-updown-5m-1",
        "question": "Bitcoin Up or Down",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "endDate": "2030-01-01T00:00:05Z",
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "acceptingOrders": True,
        "closed": False,
    }
    payload.update(overrides)
    return payload


def test_gamma_v1_and_v2_mapping_refuses_yes_no_and_unknown_version():
    event = gamma_market_event(_gamma(), synthetic=False)
    assert event is not None
    assert event["up_token_id"] == "111"
    assert event["down_token_id"] == "222"
    assert event["synthetic"] is False
    swapped = gamma_market_event(
        _gamma(outcomes='["Down", "Up"]', clobTokenIds='["222", "111"]')
    )
    assert swapped["up_token_id"] == "111"
    assert swapped["up_index"] == 1
    v2 = _gamma(
        version="v2",
        clobTokenIds=None,
        positionIds=["900", "901"],
        outcomes=["Up", "Down"],
    )
    parsed = gamma_market_event(v2)
    assert parsed["up_token_id"] == "900"
    assert gamma_market_event(_gamma(outcomes='["Yes", "No"]')) is None
    assert gamma_market_event(_gamma(version="v3")) is None
    assert gamma_market_event(_gamma(clobTokenIds='["111", "111"]')) is None


def test_resolution_and_ws_duplicates():
    market = gamma_market_event(_gamma())
    assert (
        data_api_resolution_event(
            market,
            {
                "data": [
                    {
                        "condition_id": "0xabc",
                        "status": "proposed",
                        "payouts": [1_000_000, 0],
                    }
                ]
            },
        )
        is None
    )
    assert (
        data_api_resolution_event(
            market,
            {
                "data": [
                    {
                        "condition_id": "0xabc",
                        "status": "resolved",
                        "payouts": [1, 0],
                        "market_type": "BINARY",
                    }
                ]
            },
        )
        is None
    )
    official = data_api_resolution_event(
        market,
        {
            "data": [
                {
                    "condition_id": "0xABC",
                    "status": "resolved",
                    "payouts": [0, 1_000_000],
                    "market_type": "BINARY",
                }
            ]
        },
    )
    assert official["payouts"] == [0, 1_000_000]
    assert official["source"] == "data-api-v2"
    trades = data_api_trade_events(
        market,
        {
            "data": [
                {
                    "token_id": "111",
                    "side": "BUY",
                    "price": 0.52,
                    "size": 5,
                    "timestamp": 1700000000,
                    "transaction_hash": "0x1",
                }
            ]
        },
    )
    assert trades[0]["outcome"] == "up"
    assert trades[0]["taker_side"] == "BUY"
    located = {"111": ("11", "up"), "222": ("11", "down")}
    frame = {
        "event_type": "last_trade_price",
        "asset_id": "111",
        "market": "0xabc",
        "price": "0.52",
        "size": "5",
        "side": "BUY",
        "timestamp": "1700000000000",
        "transaction_hash": "0x1",
    }
    first = events_from_ws_message(located, frame)
    second = events_from_ws_message(located, frame)
    assert first[0]["event_id"] == second[0]["event_id"]
    resolved = events_from_ws_message(
        located,
        {
            "event_type": "market_resolved",
            "id": "1",
            "market": "0xabc",
            "winning_asset_id": "111",
            "winning_outcome": "Down",
            "timestamp": "1700000000001",
        },
    )
    assert resolved[0]["source"] == "clob-market-resolved"
    assert resolved[0]["winning_outcome"] == "Down"


def test_ws_label_conflict_cash(tmp_path):
    from decimal import Decimal

    from tests.helpers import market_event, run

    engine = make_engine(tmp_path, auto_split=False)
    run(
        engine,
        [
            market_event(),
            {
                "type": "split",
                "event_id": "s",
                "ts": 1,
                "market_id": "m1",
                "amount": "10",
            },
            {
                "type": "resolution",
                "event_id": "bad-ws",
                "ts": 2,
                "market_id": "m1",
                "source": "clob-market-resolved",
                "winning_asset_id": "m1-up",
                "winning_outcome": "Down",
            },
        ],
    )
    assert engine.state.markets["m1"].winner is None
    assert engine.state.cash == Decimal("990")
    run(
        engine,
        [
            {
                "type": "resolution",
                "event_id": "good-ws",
                "ts": 3,
                "market_id": "m1",
                "source": "clob-market-resolved",
                "winning_asset_id": "m1-up",
                "winning_outcome": "Up",
            }
        ],
    )
    assert engine.state.markets["m1"].status == "redeemed"
    assert engine.state.cash == Decimal("1000")


def test_urllib_transport_refuses_other_hosts():
    try:
        UrllibTransport().get_json("https://example.com/book")
    except PublicDataError as exc:
        assert "non-official" in str(exc)
    else:
        raise AssertionError("expected refusal")


class MapTransport:
    def __init__(self):
        self.urls = []
        self.trade_calls = 0

    def get_json(self, url: str):
        self.urls.append(url)
        if url.startswith(GAMMA):
            return _gamma(endDate="2030-01-01T00:00:00Z")
        if "/book?" in url:
            return {
                "timestamp": "1700000003000",
                "hash": "book-1",
                "bids": [{"price": "0.48", "size": "100"}],
                "asks": [{"price": "0.52", "size": "10"}],
                "tick_size": "0.01",
                "min_order_size": "5",
            }
        if "/v2/trades?" in url:
            self.trade_calls += 1
            if self.trade_calls == 1:
                return {"data": []}
            return {
                "data": [
                    {
                        "token_id": "111",
                        "side": "BUY",
                        "price": "0.52",
                        "size": "100",
                        "timestamp": 1700000002,
                        "transaction_hash": "0xfill",
                    },
                    {
                        "token_id": "111",
                        "side": "BUY",
                        "price": "0.52",
                        "size": "100",
                        "timestamp": 1700000002,
                        "transaction_hash": "0xfill",
                    },
                ]
            }
        if "/v2/resolutions?" in url:
            return {"data": []}
        raise PublicDataError("unexpected url", status=404)


def test_paper_session_is_bounded_and_dedupes_trades(tmp_path):
    config = Config(
        initial_capital=__import__("decimal").Decimal("1000"),
        max_loss_fraction=__import__("decimal").Decimal("0.03"),
        stale_after_ms=10_000,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        stop_quoting_before_expiry_ms=1_000,
        paper_duration_seconds=5,
        paper_max_markets=1,
        poll_interval_seconds=2,
    )
    transport = MapTransport()
    clock = {"t": 1700000000.0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    summary = run_paper(
        config,
        tmp_path / "paper",
        duration_seconds=5,
        max_markets=1,
        client=PublicReadClient(transport),
        now_s=now,
        sleep=sleep,
    )
    assert summary["paper_only"] is True
    assert summary["live_orders"] is False
    assert summary["polls"] <= 4
    assert clock["t"] <= 1700000005
    from decimal import Decimal

    assert Decimal(summary["worst_terminal_equity"]) >= Decimal(summary["loss_floor"])
    from decimal import Decimal

    state = json.loads((tmp_path / "paper" / "state.json").read_text(encoding="utf-8"))
    assert state["markets"]
    assert "0xfill" in "".join(state["seen_trade_ids"])
    # Book exchange time is 3s ahead of the session clock; a decision still runs.
    assert any(Decimal(market["up"]) > 0 for market in state["markets"].values())


def test_cli_demo_is_idempotent_on_restart(tmp_path):
    config = load_config(ROOT / "config.paper.json")
    out = tmp_path / "demo"
    scenario = ROOT / "paper_split_maker" / "scenarios" / "symmetric_demo.json"
    run_demo(config, out, scenario)
    first = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    run_demo(config, out, scenario)
    second = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert second["resumed"] is True
    assert second["cash"] == first["cash"]
    assert second["realized_pnl"] == first["realized_pnl"]
    from decimal import Decimal

    assert Decimal(second["loss_floor"]) == Decimal("970")
    assert (out / "logs" / "events.jsonl").exists()
    assert (out / "logs" / "events.csv").exists()


def test_cli_replay_refuses_unlabeled_scenario(tmp_path):
    scenario = tmp_path / "plain.json"
    scenario.write_text(
        json.dumps({"label": "production", "events": []}), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        run_replay(
            load_config(ROOT / "config.paper.json"),
            scenario,
            tmp_path / "out",
        )


def test_package_source_has_no_live_order_stack():
    """The paper engine stays free of the trading stack. Live modules may import it."""
    live_modules = {
        "live_clob.py",
        "live_runner.py",
        "chain_send.py",
        "credentials.py",
    }
    banned = ("py_clob_client", "eth_account", "web3", 'method="POST"', "private_key")
    for path in (ROOT / "paper_split_maker").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name in live_modules:
            assert "print(self.private_key" not in text
            assert "print(creds.private_key" not in text
            continue
        for token in banned:
            assert token not in text, f"{path} contains {token}"
